#pragma once
// Internal to tas_kinds: the modules tas_kind_module() dispatches to.

#include "tas_kinds.h"

extern const tas_kind_module_t tas_mic_module;
extern const tas_kind_module_t tas_speaker_module;
extern const tas_kind_module_t tas_button_module;
extern const tas_kind_module_t tas_led_module;

// params.formats for a mic or speaker at `rate`: pcm_s16le mono, 10 or 20 ms frames.
tas_json_t *tas_kind_audio_params(uint32_t rate, tas_arena_t *arena);
