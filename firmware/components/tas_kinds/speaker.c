// The speaker Kind (docs/protocol/kinds/speaker.md). The jitter buffer, ramps, low-cut, volume, drain/drained, played_ms and the cut.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("speaker", "%s ready", cap->id);
}

const tas_kind_module_t tas_speaker_module = {.name = "speaker", .init = init};
