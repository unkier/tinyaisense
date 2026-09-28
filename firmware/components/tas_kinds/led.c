// The led Kind (docs/protocol/kinds/led.md). set/off, the brightness cap, and the Status underneath an agent claim.

#include "esp_log.h"
#include "kind_modules.h"

static void init(const tas_cap_t *cap) {
    ESP_LOGI("led", "%s ready", cap->id);
}

const tas_kind_module_t tas_led_module = {.name = "led", .init = init};
