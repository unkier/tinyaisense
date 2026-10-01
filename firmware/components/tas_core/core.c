#include "tas_core.h"

#include <stdatomic.h>

#include "core.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tas_kinds.h"

#define TAG "core"
#define HEAP_WARN_BYTES (40 * 1024)
#define HEAP_CHECK_US 10000000
#define RESET_HOLD_MS 5000

void core_breathe(void) {
    esp_task_wdt_reset();
    static _Atomic int64_t next_check;
    int64_t now = esp_timer_get_time(), due = atomic_load(&next_check);
    if (now < due) return;
    // Only one of the tasks breathing at once checks.
    if (!atomic_compare_exchange_strong(&next_check, &due, now + HEAP_CHECK_US)) return;
    size_t free = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    if (free < HEAP_WARN_BYTES) {
        ESP_LOGW(TAG, "free heap is %u B, below %u B", (unsigned)free, HEAP_WARN_BYTES);
    }
}

// Holding the button through boot for 5 s erases every setting. The LED shows the identify blink
// while it's held.
static void factory_reset_if_held(const tas_board_t *board) {
    if (!board->reset_button_down()) return;
    ESP_LOGW(TAG, "button held: keep holding for a factory reset");
    const tas_look_t *look = &board->status_looks[TAS_STATUS_IDENTIFY];
    for (int64_t start = esp_timer_get_time() / 1000, t = 0; t < RESET_HOLD_MS;
         t = esp_timer_get_time() / 1000 - start) {
        if (!board->reset_button_down()) {
            ESP_LOGI(TAG, "button let go: no reset");
            return;
        }
        status_render_look(board, look, (uint64_t)t);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    ESP_LOGW(TAG, "factory reset");
    if (!config_factory_reset()) ESP_LOGE(TAG, "the factory reset failed");
    // Show it's done, the blink held solid, until the button is let go.
    const tas_look_t done = {look->color, look->brightness, TAS_PATTERN_SOLID, 0};
    while (board->reset_button_down()) {
        status_render_look(board, &done, 0);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void tas_core_start(const tas_board_t *board) {
    board->init();
    ESP_LOGI(TAG, "board %s", board->name);
    config_init();
    factory_reset_if_held(board);
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        tas_kind_module(cap->kind)->init(board, cap);
    }
    commands_init(board);
    const tas_manifest_t *manifest = manifest_build(board);
    status_start(board);
    net_start();
    ctrl_start(manifest);
    console_start(board, manifest);
}
