// The button Kind (docs/protocol/kinds/button.md). Edges, the Reflex, and the UDP event mirror.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("button", "%s ready", cap->id);
}

const tas_kind_module_t tas_button_module = {.name = "button", .init = init};
