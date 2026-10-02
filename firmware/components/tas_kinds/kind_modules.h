#pragma once
// Internal to tas_kinds: the modules tas_kind_module() dispatches to, and what they share.

#include "tas_jbuf.h"
#include "tas_kinds.h"
#include "tas_reflex.h"

#define TAS_KINDS_MAX_INSTANCES 4 // the most capabilities of one Kind a Board may have
#define TAS_KIND_FRAME_MS_COUNT 2

extern const tas_kind_module_t tas_mic_module;
extern const tas_kind_module_t tas_speaker_module;
extern const tas_kind_module_t tas_button_module;
extern const tas_kind_module_t tas_led_module;

// The core's hooks, from tas_kinds_init.
extern const tas_kinds_core_t *tas_kinds_core;

// The frame durations a mic or speaker advertises, in ms.
extern const uint32_t tas_kind_frame_ms[TAS_KIND_FRAME_MS_COUNT];

// params.formats for a mic or speaker at `rate`: pcm_s16le mono, 10 or 20 ms frames.
tas_json_t *tas_kind_audio_params(uint32_t rate, tas_arena_t *arena);
// Reads a mic's or speaker's start args `stream` and `format`, for a capability at `rate`. False,
// with `reply` failed, if they break the contract.
bool tas_kind_stream_args(const tas_json_t *args, uint32_t rate, uint8_t *stream,
                          uint32_t *frame_ms, tas_msg_t *reply);
// A start's reply: `{format}`, the format chosen.
void tas_kind_format_reply(uint32_t rate, uint32_t frame_ms, tas_arena_t *arena, tas_msg_t *reply);
// Stream `stream` closed: its late frames are dropped silently for the rest of the Connection.
void tas_kind_mark_closed(uint8_t stream);

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

// ---- exclusive.c: the exclusive groups (tas_board_t.exclusive)

// Before any capability is set up.
void tas_exclusive_init(const tas_board_t *board);
// Marks `cap` active, for a stream. False if a peer in one of its exclusive groups is active.
bool tas_kind_acquire(const tas_cap_t *cap);
void tas_kind_release(const tas_cap_t *cap);
// Hands what `cap`'s exclusive groups share (the Board's switch hook) to `cap`, if it's still
// active, once its peers are quiet. It waits only for a speaker peer's cut to play out, a few ms
// at most, so call it before starting `cap`'s hardware: from the task that drives it, or from
// the ctrl task if `cap` is a speaker, whose peers are mics.
void tas_kind_take_pins(const tas_cap_t *cap);

// ---- speaker.c: playout, for the speaker's streams and the test hooks

// Opens stream `stream` (a hub-range id, 1-127) on the speaker `cap`. False if a stream is open
// already, an exclusive peer is active, the jitter buffer can't take `cfg`, or its frames don't
// split into DMA descriptors.
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
// isn't known yet, by stopping the channel. Never blocks for long. The speaker logs when a patched
// cut went silent, timed from the call.
tas_speaker_cut_t tas_speaker_cut(const tas_cap_t *cap, bool hard);
// The Reflex's cut: as tas_speaker_cut by DMA patch, but timed from the press at `edge_us`. False
// if no stream was open; else its id, and what was heard of it.
bool tas_speaker_reflex_cut(const tas_cap_t *cap, uint64_t edge_us, uint8_t *stream,
                            tas_jbuf_stats_t *heard);
// How long after the call the last patched cut went silent. False until the DMA gets there.
bool tas_speaker_cut_silent_after(const tas_cap_t *cap, uint32_t *us);

// ---- mic.c: the Reflex's streams, on device-range ids

// The id of the stream the mic `cap` has open, hub or Reflex. False if none.
bool tas_mic_stream(const tas_cap_t *cap, uint8_t *stream);
// Opens the mic `cap` on the device-range `stream`, with `frame_ms` frames. False if a stream is
// open, or an exclusive peer is active.
bool tas_mic_open(const tas_cap_t *cap, uint8_t stream, uint32_t frame_ms);
// Closes the mic `cap`'s stream `stream` once its last frame has gone out: a few ms. False if it
// has no stream of that id open.
bool tas_mic_close(const tas_cap_t *cap, uint8_t stream, tas_mic_end_t *end);

// ---- button.c

#if CONFIG_TAS_TEST_HOOKS
// Injects an edge into the button `cap` as its interrupt would, now, with its pin held at `down`
// until the next injection: up hands the pin back to the hardware.
void tas_button_inject(const tas_cap_t *cap, bool down);
#endif
