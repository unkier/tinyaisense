#pragma once
// Internal to tas_kinds: the modules tas_kind_module() dispatches to, and what they share.

#include "tas_kinds.h"

#define TAS_KINDS_MAX_INSTANCES 4 // the most capabilities of one Kind a Board may have

extern const tas_kind_module_t tas_mic_module;
extern const tas_kind_module_t tas_speaker_module;
extern const tas_kind_module_t tas_button_module;
extern const tas_kind_module_t tas_led_module;

// params.formats for a mic or speaker at `rate`: pcm_s16le mono, 10 or 20 ms frames.
tas_json_t *tas_kind_audio_params(uint32_t rate, tas_arena_t *arena);

// The capabilities a Kind module runs an instance for, in the order they were set up.
typedef struct {
    const tas_cap_t *caps[TAS_KINDS_MAX_INSTANCES];
    size_t count;
} tas_kind_instances_t;

// Adds `cap` and returns its index. Call it from init, before any task runs. Aborts past
// TAS_KINDS_MAX_INSTANCES.
size_t tas_kind_add(tas_kind_instances_t *instances, const tas_cap_t *cap);
// The index of `cap`. Aborts if it isn't there: every capability is set up at boot.
size_t tas_kind_find(const tas_kind_instances_t *instances, const tas_cap_t *cap);
