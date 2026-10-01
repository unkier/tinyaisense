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

tas_json_t *tas_kind_audio_params(uint32_t rate, tas_arena_t *arena) {
    tas_json_t *frame_ms =
        tas_json_push(tas_json_push(tas_json_array(arena), tas_json_int(arena, 10)),
                      tas_json_int(arena, 20));
    tas_json_t *format = tas_json_object(arena);
    format = tas_json_put(arena, format, "codec", tas_json_string(arena, "pcm_s16le"));
    format = tas_json_put(arena, format, "rate", tas_json_int(arena, rate));
    format = tas_json_put(arena, format, "channels", tas_json_int(arena, 1));
    format = tas_json_put(arena, format, "frame_ms", frame_ms);
    tas_json_t *formats = tas_json_push(tas_json_array(arena), format);
    return tas_json_put(arena, tas_json_object(arena), "formats", formats);
}
