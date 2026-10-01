#include <string.h>

#include "core.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define FRAME_MS 20 // 50 Hz
#define LED_CORE 1
#define LED_PRIORITY 5
#define LED_STACK 3072

static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
static bool wifi_configured, wifi_up;
static ctrl_state_t conn;
static tas_status_t hub_layer = TAS_STATUS_NO_AGENT;

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

tas_status_t status_shown(void) {
    portENTER_CRITICAL(&mux);
    tas_status_layers_t layers = {
        .no_config = !wifi_configured,
        .offline = wifi_configured && !wifi_up,
        .connecting = wifi_up && conn != CTRL_PENDING && conn != CTRL_WELCOMED,
        .pending = wifi_up && conn == CTRL_PENDING,
        .hub = hub_layer,
    };
    portEXIT_CRITICAL(&mux);
    return tas_status_shown(&layers);
}

static const tas_cap_t *status_cap(const tas_board_t *board) {
    for (size_t i = 0; i < board->cap_count; i++) {
        if (!strcmp(board->caps[i].id, board->status_led)) return &board->caps[i];
    }
    return NULL;
}

static tas_rgb_t shade(const tas_board_t *board, const tas_look_t *look, uint64_t t_ms) {
    uint32_t level = tas_pattern_level(look->pattern, look->period_ms, t_ms) * look->brightness *
                     board->led_brightness_cap / (100 * 255);
    return (tas_rgb_t){(uint8_t)(look->color.r * level / 255),
                       (uint8_t)(look->color.g * level / 255),
                       (uint8_t)(look->color.b * level / 255)};
}

static void show(const tas_board_t *board, const tas_cap_t *cap, tas_rgb_t rgb) {
    for (size_t i = 0; i < cap->count; i++) board->led_set(cap, i, rgb);
    board->led_show(cap);
}

void status_render_look(const tas_board_t *board, const tas_look_t *look, uint64_t t_ms) {
    const tas_cap_t *cap = status_cap(board);
    if (cap) show(board, cap, shade(board, look, t_ms));
}

static void led_task(void *arg) {
    const tas_board_t *board = arg;
    const tas_cap_t *cap = status_cap(board);
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    tas_rgb_t shown = {0};
    bool drawn = false;
    TickType_t wake = xTaskGetTickCount();
    for (;;) {
        esp_task_wdt_reset();
        tas_status_t state = status_shown();
        // An agent's claim shows what the agent set: the led Kind's to render.
        if (cap && state != TAS_STATUS_CLAIM) {
            uint64_t t_ms = (uint64_t)xTaskGetTickCount() * portTICK_PERIOD_MS;
            tas_rgb_t rgb = shade(board, &board->status_looks[state], t_ms);
            if (!drawn || memcmp(&rgb, &shown, sizeof rgb) != 0) {
                show(board, cap, rgb);
                shown = rgb;
                drawn = true;
            }
        }
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(FRAME_MS));
    }
}

void status_start(const tas_board_t *board) {
    // Offline until WiFi comes up, not no_config, when there's WiFi to join.
    status_set_wifi(config_get().ssid[0] != 0, false);
    xTaskCreatePinnedToCore(led_task, "led", LED_STACK, (void *)board, LED_PRIORITY, NULL,
                            LED_CORE);
}
