// The button Kind (docs/protocol/kinds/button.md). Edges and the Reflex.
//
// Each button's reflex task takes its edges: the button's ISR notifies it, and it confirms
// each edge by re-reading the pin (tas_debounce), timed by an esp_timer so the confirm costs ~1 ms
// and no tick. A confirmed edge goes to the Reflex (tas_reflex), which cuts the speakers, opens the
// mic and queues the events, the button's mirrored over UDP. The task is the highest-priority one
// on the audio core, so a press beats playout and capture to it.

#include <stdatomic.h>
#include <string.h>

#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "kind_modules.h"
#include "tas_debounce.h"
#include "tas_reflex.h"

#define TAG "button"
#define REFLEX_CORE 1
#define REFLEX_PRIORITY 20
#define REFLEX_STACK 3072
#define WAIT_MS 1000 // the longest a wait goes between watchdog feeds

typedef struct {
    const tas_cap_t *cap;
    TaskHandle_t task;
    esp_timer_handle_t timer; // wakes the task at the debounce's deadline
    tas_debounce_t debounce;  // the task's
    portMUX_TYPE isr_lock;    // the edges since the task last took them
    bool edges;
    int64_t first_us, last_us;
    atomic_bool held; // the test hooks hold it down
    SemaphoreHandle_t lock; // the Reflex: the task's edges, and the hub's commands from ctrl
    StaticSemaphore_t lock_buf;
    tas_reflex_t reflex;
} button_t;

static tas_kind_instances_t instances;
static button_t buttons[TAS_KINDS_MAX_INSTANCES];

static button_t *button_of(const tas_cap_t *cap) {
    return &buttons[tas_kind_find(&instances, cap)];
}

// Notes an edge at `at_us`, from the ISR or the test hooks; then the task wants waking.
static void note_edge(button_t *b, int64_t at_us) {
    portENTER_CRITICAL_SAFE(&b->isr_lock);
    if (!b->edges) b->first_us = at_us;
    b->last_us = at_us;
    b->edges = true;
    portEXIT_CRITICAL_SAFE(&b->isr_lock);
}

static void on_edge(void *ctx) {
    button_t *b = ctx;
    note_edge(b, esp_timer_get_time());
    BaseType_t woke = pdFALSE;
    vTaskNotifyGiveFromISR(b->task, &woke);
    portYIELD_FROM_ISR(woke);
}

static void on_deadline(void *ctx) {
    xTaskNotifyGive(((button_t *)ctx)->task);
}

static bool pin_down(button_t *b) {
    return atomic_load(&b->held) || b->cap->button->down();
}

// Hands the edges since the last take to the debounce: the first, and the last if it's another.
static void take_edges(button_t *b) {
    taskENTER_CRITICAL(&b->isr_lock);
    bool edges = b->edges;
    int64_t first_us = b->first_us, last_us = b->last_us;
    b->edges = false;
    taskEXIT_CRITICAL(&b->isr_lock);
    if (!edges) return;
    tas_debounce_edge(&b->debounce, (uint64_t)first_us);
    if (last_us != first_us) tas_debounce_edge(&b->debounce, (uint64_t)last_us);
}

static void reflex_task(void *arg) {
    button_t *b = arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    tas_debounce_init(&b->debounce, pin_down(b));
    b->cap->button->open(on_edge, b); // its ISR on this core
    for (;;) {
        esp_task_wdt_reset();
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(WAIT_MS));
        take_edges(b);
        uint64_t now = (uint64_t)esp_timer_get_time();
        tas_edge_t edge;
        if (now >= tas_debounce_deadline(&b->debounce) &&
            tas_debounce_check(&b->debounce, now, pin_down(b), &edge)) {
            xSemaphoreTake(b->lock, portMAX_DELAY);
            tas_reflex_edge(&b->reflex, &edge, (uint64_t)esp_timer_get_time());
            xSemaphoreGive(b->lock);
        }
        uint64_t deadline = tas_debounce_deadline(&b->debounce);
        if (deadline == TAS_DEBOUNCE_NONE) continue;
        now = (uint64_t)esp_timer_get_time();
        esp_timer_stop(b->timer); // fails harmlessly if it isn't running
        if (deadline <= now) xTaskNotifyGive(b->task);
        else ESP_ERROR_CHECK(esp_timer_start_once(b->timer, deadline - now));
    }
}

// ---- What the Reflex does to the device

static bool cut(void *ctx, const tas_cap_t *cap, uint64_t edge_us, uint8_t *stream,
                tas_jbuf_stats_t *heard) {
    (void)ctx;
    return tas_speaker_reflex_cut(cap, edge_us, stream, heard);
}

static bool mic_stream(void *ctx, const tas_cap_t *cap, uint8_t *stream) {
    (void)ctx;
    return tas_mic_stream(cap, stream);
}

static bool mic_open(void *ctx, const tas_cap_t *cap, uint8_t stream, uint32_t frame_ms) {
    (void)ctx;
    return tas_mic_open(cap, stream, frame_ms);
}

static bool mic_close(void *ctx, const tas_cap_t *cap, uint8_t stream, tas_mic_end_t *end) {
    (void)ctx;
    return tas_mic_close(cap, stream, end);
}

static bool emit(void *ctx, const char *cap, const char *name, uint64_t ts, const uint8_t *data,
                 size_t len, bool mirror) {
    (void)ctx;
    tas_kind_event_t event = {.cap = cap, .name = name, .ts = ts, .len = len};
    if (len > sizeof event.data) return false;
    memcpy(event.data, data, len);
    return tas_kinds_core->emit(&event, mirror);
}

// ---- The Kind module

static void init(const tas_board_t *board, const tas_cap_t *cap) {
    button_t *b = &buttons[tas_kind_add(&instances, cap)];
    b->cap = cap;
    portMUX_INITIALIZE(&b->isr_lock);
    b->lock = xSemaphoreCreateMutexStatic(&b->lock_buf);
    tas_reflex_config_t cfg = {.board = board,
                               .button = cap,
                               .frame_ms = tas_kind_frame_ms,
                               .frame_ms_count = TAS_KIND_FRAME_MS_COUNT,
                               .ops = {.cut = cut,
                                       .mic_stream = mic_stream,
                                       .mic_open = mic_open,
                                       .mic_close = mic_close,
                                       .emit = emit}};
    tas_reflex_init(&b->reflex, &cfg);
    esp_timer_create_args_t timer = {.callback = on_deadline, .arg = b, .name = "reflex"};
    ESP_ERROR_CHECK(esp_timer_create(&timer, &b->timer));
    xTaskCreatePinnedToCore(reflex_task, "reflex", REFLEX_STACK, b, REFLEX_PRIORITY, &b->task,
                            REFLEX_CORE);
    ESP_LOGI(TAG, "%s ready", cap->id);
}

static void command(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply) {
    (void)arena;
    if (!tas_str_eq(cmd->cmd.name, "set_reflex")) {
        return tas_reply_fail(reply, "unsupported", "button has no such command");
    }
    button_t *b = button_of(cap);
    xSemaphoreTake(b->lock, portMAX_DELAY);
    tas_reflex_set(&b->reflex, cmd->cmd.args, reply);
    xSemaphoreGive(b->lock);
}

// The Reflex disarms, and a button held now is ignored until it's let go. Once a press it was
// handling is done, so what it opened ends with the other capabilities.
static void disconnected(const tas_cap_t *cap) {
    button_t *b = button_of(cap);
    xSemaphoreTake(b->lock, portMAX_DELAY);
    tas_reflex_disconnected(&b->reflex);
    xSemaphoreGive(b->lock);
}

bool tas_kinds_reflex_armed(const tas_cap_t *cap) {
    button_t *b = button_of(cap);
    xSemaphoreTake(b->lock, portMAX_DELAY);
    bool armed = b->reflex.armed;
    xSemaphoreGive(b->lock);
    return armed;
}

#if CONFIG_TAS_TEST_HOOKS
void tas_button_inject(const tas_cap_t *cap, bool down) {
    button_t *b = button_of(cap);
    atomic_store(&b->held, down);
    note_edge(b, esp_timer_get_time());
    xTaskNotifyGive(b->task);
}
#endif

const tas_kind_module_t tas_button_module = {
    .name = "button", .init = init, .command = command, .disconnected = disconnected};
