// The speaker Kind (docs/protocol/kinds/speaker.md). The jitter buffer, ramps, low-cut, volume, drain/drained, played_ms and the cut.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("speaker", "%s ready", cap->id);
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)cap;
    return tas_kind_audio_params(board->spk_cond.rate, arena);
}

const tas_kind_module_t tas_speaker_module = {.name = "speaker", .init = init, .params = params};
