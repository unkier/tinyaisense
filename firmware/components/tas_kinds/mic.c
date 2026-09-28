// The mic Kind (docs/protocol/kinds/mic.md). Conditioning, the 200 ms queue, last_seq and dropped.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("mic", "%s ready", cap->id);
}

const tas_kind_module_t tas_mic_module = {.name = "mic", .init = init};
