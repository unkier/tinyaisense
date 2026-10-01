#pragma once
// Internal to tas_kinds: the modules tas_kind_module() dispatches to, and what they share.

#include "tas_jbuf.h"
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

// ---- speaker.c: playout, for the speaker's streams and the test hooks

#define TAS_SPEAKER_MAX_FRAME 320 // samples: the longest frame advertised, 20 ms at 16 kHz

// Opens stream `stream` (a hub-range id, 1-127) on the speaker `cap`. False if a stream is open
// already, the jitter buffer can't take `cfg`, or its frames don't split into DMA descriptors.
bool tas_speaker_open(const tas_cap_t *cap, uint8_t stream, const tas_jbuf_config_t *cfg);
// Hands the open stream its frame `seq`. False if it's dropped: a late frame, or not the open
// stream's, e.g. one of a stream already cut.
bool tas_speaker_push(const tas_cap_t *cap, uint8_t stream, uint32_t seq, const int16_t *pcm,
                      size_t samples);

typedef enum {
    TAS_SPEAKER_IDLE,    // no stream was open: nothing to do
    TAS_SPEAKER_PATCHED, // by DMA patch: silent within about a descriptor and a ramp
    TAS_SPEAKER_STOPPED, // by stopping the channel, which clicks
} tas_speaker_cut_t;

// Ends the open stream and silences the speaker at once: by DMA patch; or, if `hard` or the ring
// isn't known yet, by stopping the channel. Never blocks for long, so the Reflex can call it. The
// speaker logs when a patched cut went silent, timed from the call.
tas_speaker_cut_t tas_speaker_cut(const tas_cap_t *cap, bool hard);
// How long after the call the last patched cut went silent. False until the DMA gets there.
bool tas_speaker_cut_silent_after(const tas_cap_t *cap, uint32_t *us);
