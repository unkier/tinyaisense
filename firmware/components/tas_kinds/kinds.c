#include "kind_modules.h"

const tas_kind_module_t *tas_kind_module(tas_kind_t kind) {
    switch (kind) {
    case TAS_KIND_MIC: return &tas_mic_module;
    case TAS_KIND_SPEAKER: return &tas_speaker_module;
    case TAS_KIND_BUTTON: return &tas_button_module;
    case TAS_KIND_LED: return &tas_led_module;
    }
    return NULL;
}
