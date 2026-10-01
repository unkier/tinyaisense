#include <stdlib.h>
#include <string.h>

#include "core.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tas_kinds.h"

#define FRAME_MS 20 // 50 Hz
#define LED_CORE 1
#define LED_PRIORITY 5
#define LED_STACK 3072

static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
static bool wifi_configured, wifi_up;
static ctrl_state_t conn;
static tas_status_t hub_layer = TAS_STATUS_NO_AGENT;
static uint64_t identify_from_ms, identify_until_ms;

static const tas_cap_t *status_cap; // NULL if the Board shows no Status

static uint64_t now_ms(void) {
    return (uint64_t)esp_timer_get_time() / 1000;
}

void status_set_wifi(bool configured, bool up) {
    portENTER_CRITICAL(&mux);
    wifi_configured = configured;
    wifi_up = up;
    portEXIT_CRITICAL(&mux);
}

void status_set_conn(ctrl_state_t state) {
    portENTER_CRITICAL(&mux);
    conn = state;
    // The hub layer starts at no_agent on every welcome.
    if (state != CTRL_WELCOMED) hub_layer = TAS_STATUS_NO_AGENT;
    portEXIT_CRITICAL(&mux);
}

void status_set_hub(tas_status_t layer) {
    portENTER_CRITICAL(&mux);
    hub_layer = layer;
    portEXIT_CRITICAL(&mux);
}

void status_identify(uint32_t s) {
    uint64_t now = now_ms();
    portENTER_CRITICAL(&mux);
    identify_from_ms = now;
    identify_until_ms = now + (uint64_t)s * 1000;
    portEXIT_CRITICAL(&mux);
}

// The Status shown at `now`, with the Status LED claimed or not, and when it started showing if
// it's identify: a new identify restarts the blink.
static tas_status_t shown(bool claimed, uint64_t now, uint64_t *identify_from) {
    bool listening = tas_kinds_streaming(TAS_KIND_MIC);
    bool speaking = tas_kinds_streaming(TAS_KIND_SPEAKER);
    portENTER_CRITICAL(&mux);
    tas_status_layers_t layers = {
        .identify = now < identify_until_ms,
        .listening = listening,
        .speaking = speaking,
        .claimed = claimed,
        .no_config = !wifi_configured,
        .offline = wifi_configured && !wifi_up,
        .connecting = wifi_up && conn != CTRL_PENDING && conn != CTRL_WELCOMED,
        .pending = wifi_up && conn == CTRL_PENDING,
        .hub = hub_layer,
    };
    *identify_from = identify_from_ms;
    portEXIT_CRITICAL(&mux);
    return tas_status_shown(&layers);
}

tas_status_t status_shown(void) {
    tas_led_t led = {0};
    if (status_cap) tas_kinds_led(status_cap, &led);
    uint64_t identify_from;
    return shown(led.claimed, now_ms(), &identify_from);
}

static void show(const tas_board_t *board, const tas_cap_t *cap, const tas_rgb_t *rgb) {
    for (size_t i = 0; i < cap->count; i++) board->led_set(cap, i, rgb[i]);
    board->led_show(cap);
}

static const tas_cap_t *find_status_cap(const tas_board_t *board) {
    for (size_t i = 0; i < board->cap_count; i++) {
        if (!strcmp(board->caps[i].id, board->status_led)) return &board->caps[i];
    }
    return NULL;
}

void status_render_look(const tas_board_t *board, const tas_look_t *look, uint64_t t_ms) {
    const tas_cap_t *cap = find_status_cap(board);
    if (!cap || cap->count > TAS_LED_MAX) return;
    tas_rgb_t rgb[TAS_LED_MAX];
    for (size_t i = 0; i < cap->count; i++) {
        rgb[i] = tas_look_rgb(look, board->led_brightness_cap, t_ms);
    }
    show(board, cap, rgb);
}

// What the LEDs of `cap` show at `now`: the Status on the Status LED, unless the agent's claim
// outranks it; on any other, the claim, or off.
static void render(const tas_board_t *board, const tas_cap_t *cap, uint64_t now, tas_rgb_t *rgb) {
    tas_led_t led;
    tas_kinds_led(cap, &led);
    if (cap == status_cap) {
        uint64_t identify_from;
        tas_status_t state = shown(led.claimed, now, &identify_from);
        if (state != TAS_STATUS_CLAIM) {
            uint64_t from = state == TAS_STATUS_IDENTIFY && identify_from < now ? identify_from : 0;
            tas_rgb_t color =
                tas_look_rgb(&board->status_looks[state], board->led_brightness_cap, now - from);
            for (size_t i = 0; i < cap->count; i++) rgb[i] = color;
            return;
        }
    }
    for (size_t i = 0; i < cap->count; i++) {
        rgb[i] = tas_led_rgb(&led, i, board->led_brightness_cap, now);
    }
}

// What a capability's LEDs were last sent.
typedef struct {
    tas_rgb_t rgb[TAS_LED_MAX];
    bool sent; // false until the first time
} lit_t;

static void led_task(void *arg) {
    const tas_board_t *board = arg;
    lit_t *lit = calloc(board->cap_count, sizeof *lit);
    if (!lit) abort();
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    TickType_t wake = xTaskGetTickCount();
    for (;;) {
        esp_task_wdt_reset();
        uint64_t now = now_ms();
        for (size_t c = 0; c < board->cap_count; c++) {
            const tas_cap_t *cap = &board->caps[c];
            if (cap->kind != TAS_KIND_LED) continue;
            tas_rgb_t rgb[TAS_LED_MAX];
            render(board, cap, now, rgb);
            size_t size = cap->count * sizeof *rgb;
            if (!lit[c].sent || memcmp(rgb, lit[c].rgb, size) != 0) {
                show(board, cap, rgb);
                memcpy(lit[c].rgb, rgb, size);
                lit[c].sent = true;
            }
        }
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(FRAME_MS));
    }
}

void status_start(const tas_board_t *board) {
    status_cap = find_status_cap(board);
    // Offline until WiFi comes up, not no_config, when there's WiFi to join.
    status_set_wifi(config_get().ssid[0] != 0, false);
    xTaskCreatePinnedToCore(led_task, "led", LED_STACK, (void *)board, LED_PRIORITY, NULL,
                            LED_CORE);
}
