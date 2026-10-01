// The mic Kind (docs/protocol/kinds/mic.md). Conditioning, the 200 ms queue, last_seq and dropped.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("mic", "%s ready", cap->id);
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)cap;
    return tas_kind_audio_params(board->mic_cond.rate, arena);
}

const tas_kind_module_t tas_mic_module = {.name = "mic", .init = init, .params = params};
