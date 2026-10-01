// The led Kind (docs/protocol/kinds/led.md). set/off, the brightness cap, and the Status underneath an agent claim.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("led", "%s ready", cap->id);
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)board;
    return tas_json_put(arena, tas_json_object(arena), "count", tas_json_int(arena, cap->count));
}

const tas_kind_module_t tas_led_module = {.name = "led", .init = init, .params = params};
