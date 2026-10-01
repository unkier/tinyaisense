// The mic Kind (docs/protocol/kinds/mic.md). Conditioning, the 200 ms queue, last_seq and dropped.
//
// Each mic's mic task captures while a stream is open: the Board's I2S channel, conditioning, then
// the stream's queue, which the core's udp_tx task empties. The channel runs only while the mic
// streams. The task restarts it for each stream: start and stop bump `gen`, and capture from an
// older one is dropped.

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "kind_modules.h"
#include "sdkconfig.h"
#include "tas_dsp.h"
#include "tas_micq.h"

#define TAG "mic"
#define MIC_CORE 1
#define MIC_PRIORITY 19
#define MIC_STACK 3072
#define CAPACITY_MS 200
#define READ_TIMEOUT_MS 100 // far past a frame: the channel has stalled

typedef struct {
    const tas_cap_t *cap;
    tas_mic_cond_config_t cond_cfg;
    TaskHandle_t task;
    tas_mic_cond_t cond;                  // the mic task's
    int16_t frame[TAS_KINDS_MAX_FRAME];   // the mic task's
    SemaphoreHandle_t lock; // everything below
    StaticSemaphore_t lock_buf;
    tas_micq_t queue;
    tas_micq_config_t queue_cfg;
    bool open;        // a stream is open
    uint8_t stream;   // its id, or the last one's while its queue empties
    uint32_t gen;     // bumped by every start and stop
} mic_t;

static tas_kind_instances_t instances;
static mic_t mics[CONFIG_TAS_MICS_MAX];

static mic_t *mic_of(const tas_cap_t *cap) {
    return &mics[tas_kind_find(&instances, cap)];
}

static void mic_task(void *arg) {
    mic_t *m = arg;
    const tas_mic_hw_t *hw = m->cap->mic;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    uint32_t gen = 0;
    bool running = false;
    size_t samples = 0;
    int64_t frame_us = 0;
    for (;;) {
        esp_task_wdt_reset();
        xSemaphoreTake(m->lock, portMAX_DELAY);
        uint32_t now_gen = m->gen;
        bool open = m->open;
        size_t frame_samples = m->queue.frame_samples;
        xSemaphoreGive(m->lock);
        if (now_gen != gen) {
            gen = now_gen;
            if (running) hw->stop();
            running = false;
            if (open) {
                tas_kind_take_pins(m->cap); // a speaker's cut plays out first
                tas_mic_cond_start(&m->cond, &m->cond_cfg);
                samples = frame_samples;
                frame_us = (int64_t)samples * 1000000 / m->cond_cfg.rate;
                hw->start();
                running = true;
            }
        }
        if (!running) {
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000));
            continue;
        }
        if (!hw->read(m->frame, samples, READ_TIMEOUT_MS)) {
            ESP_LOGW(TAG, "%s: no audio for %d ms", m->cap->id, READ_TIMEOUT_MS);
            continue;
        }
        // The read returns as the DMA completes the frame's last sample.
        uint64_t ts = (uint64_t)(esp_timer_get_time() - frame_us);
        tas_mic_cond_run(&m->cond, m->frame, samples);
        xSemaphoreTake(m->lock, portMAX_DELAY);
        bool current = m->gen == gen;
        if (current) tas_micq_push(&m->queue, m->frame, ts);
        xSemaphoreGive(m->lock);
        if (current) tas_kinds_core->frames_ready();
    }
}

static void init(const tas_board_t *board, const tas_cap_t *cap) {
    size_t i = tas_kind_add(&instances, cap);
    if (i >= CONFIG_TAS_MICS_MAX) abort();
    mic_t *m = &mics[i];
    m->cap = cap;
    m->cond_cfg = board->mic_cond;
    m->lock = xSemaphoreCreateMutexStatic(&m->lock_buf);
    m->queue_cfg = (tas_micq_config_t){
        .rate = board->mic_cond.rate, .frame_ms = tas_kind_frame_ms[0], .capacity_ms = CAPACITY_MS};
    if (!tas_micq_start(&m->queue, &m->queue_cfg)) abort();
    xTaskCreatePinnedToCore(mic_task, "mic", MIC_STACK, m, MIC_PRIORITY, &m->task, MIC_CORE);
    ESP_LOGI(TAG, "%s ready", cap->id);
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)cap;
    return tas_kind_audio_params(board->mic_cond.rate, arena);
}

static bool frame_out(const tas_cap_t *cap, tas_mic_frame_t *frame) {
    mic_t *m = mic_of(cap);
    tas_micq_frame_t queued;
    xSemaphoreTake(m->lock, portMAX_DELAY);
    bool got = tas_micq_pop(&m->queue, &queued);
    if (got) {
        frame->stream = m->stream;
        frame->seq = queued.seq;
        frame->ts = queued.ts;
        frame->samples = queued.samples;
        memcpy(frame->pcm, queued.pcm, queued.samples * sizeof *queued.pcm);
    }
    xSemaphoreGive(m->lock);
    return got;
}

// `start{stream, format}` → `{format}`.
static void start(mic_t *m, const tas_json_t *args, tas_arena_t *arena, tas_msg_t *reply) {
    uint8_t stream;
    uint32_t frame_ms, rate = m->cond_cfg.rate;
    if (!tas_kind_stream_args(args, rate, &stream, &frame_ms, reply)) return;
    tas_micq_config_t cfg = {.rate = rate, .frame_ms = frame_ms, .capacity_ms = CAPACITY_MS};
    const char *busy = NULL;
    xSemaphoreTake(m->lock, portMAX_DELAY);
    if (m->open) {
        busy = "the mic is streaming";
    } else if (!tas_kind_acquire(m->cap)) {
        busy = "an exclusive peer is active";
    } else {
        m->queue_cfg = cfg;
        tas_micq_start(&m->queue, &cfg); // fits: every format advertised does
        m->stream = stream;
        m->open = true;
        m->gen++;
    }
    xSemaphoreGive(m->lock);
    if (busy) return tas_reply_fail(reply, "busy", busy);
    xTaskNotifyGive(m->task);
    tas_kind_format_reply(rate, frame_ms, arena, reply);
}

// `stop{}` → `{last_seq, dropped}`, once the stream's last frame has gone out: a few ms.
static void stop(mic_t *m, tas_arena_t *a, tas_msg_t *reply) {
    xSemaphoreTake(m->lock, portMAX_DELAY);
    bool was_open = m->open;
    m->open = false;
    m->gen++;
    xSemaphoreGive(m->lock);
    uint32_t last_seq = 0, dropped = 0;
    bool any = false;
    if (was_open) {
        tas_kinds_core->send_frames();
        xSemaphoreTake(m->lock, portMAX_DELAY);
        any = tas_micq_last_seq(&m->queue, &last_seq);
        dropped = m->queue.dropped;
        xSemaphoreGive(m->lock);
        tas_kind_release(m->cap);
    }
    tas_json_t *r = tas_json_object(a);
    r = tas_json_put(a, r, "last_seq", any ? tas_json_int(a, last_seq) : tas_json_null(a));
    r = tas_json_put(a, r, "dropped", tas_json_int(a, dropped));
    if (!r) return tas_reply_fail(reply, "failed", "out of memory");
    reply->reply.result = r;
}

static void command(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply) {
    mic_t *m = mic_of(cap);
    if (tas_str_eq(cmd->cmd.name, "start")) return start(m, cmd->cmd.args, arena, reply);
    if (tas_str_eq(cmd->cmd.name, "stop")) return stop(m, arena, reply);
    tas_reply_fail(reply, "unsupported", "mic has no such command");
}

// The Connection's stream ends with it: what's still queued has nowhere to go.
static void disconnected(const tas_cap_t *cap) {
    mic_t *m = mic_of(cap);
    xSemaphoreTake(m->lock, portMAX_DELAY);
    if (m->open) tas_kind_release(m->cap);
    m->open = false;
    m->gen++;
    tas_micq_start(&m->queue, &m->queue_cfg);
    xSemaphoreGive(m->lock);
}

const tas_kind_module_t tas_mic_module = {.name = "mic",
                                          .init = init,
                                          .params = params,
                                          .command = command,
                                          .disconnected = disconnected,
                                          .frame_out = frame_out};
