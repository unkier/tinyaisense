#include "tas_core.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "tas_kinds.h"

#define TAG "core"
#define HEAP_WARN_BYTES (40 * 1024)

static void check_heap(void) {
    size_t free = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    if (free < HEAP_WARN_BYTES) {
        ESP_LOGW(TAG, "free heap is %u B, below %u B", (unsigned)free, HEAP_WARN_BYTES);
    }
}

void tas_core_start(const tas_board_t *board) {
    board->init();
    ESP_LOGI(TAG, "board %s", board->name);
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        tas_kind_module(cap->kind)->init(cap);
    }
    check_heap();
}
