#pragma once
// The speaker's elastic jitter buffer (docs/protocol/audio.md, "Speaker stream").
//
// The udp_rx task pushes stream frames by seq; the playout task pulls one frame per frame period,
// whatever the buffer holds, and gets PCM with every edge already ramped. All storage is inside
// the struct, so a static instance needs no malloc. Not thread-safe: the caller serializes.
//
// - Playback starts once prefill_ms is buffered (at least one frame, at most capacity_ms), or at
//   once after drain. A drain with no frames at all never starts: the caller handles that stream.
// - A missing frame while later frames are buffered is lost: concealed as silence, skipped.
// - An empty buffer is an underrun: playback pauses, and resumes at the oldest frame held once one
//   arrives. The pause stood in for the frames still missing, so they're skipped and count as lost.
// - After drain, playback never pauses: missing frames are concealed until last_seq.
// - A frame capacity_ms or more ahead of the playhead drops the frames the buffer no longer spans,
//   and the playhead jumps to the oldest frame still held. The frames it passes count as lost.
// - A short frame is legal only as the final one. It is held until the drain says whether it is,
//   and dropped (counted, then concealed) as soon as it can't be.
// - played_ms and DONE are pull-side: the speaker module subtracts what is still in the DMA ring,
//   or was cut from it.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TAS_JBUF_MAX_SAMPLES 3200 // 200 ms of mono at 16 kHz
#define TAS_JBUF_MAX_SLOTS 20     // 200 ms of 10 ms frames

typedef struct {
    uint32_t rate;        // samples per second, mono
    uint32_t frame_ms;    // the stream's frame duration
    uint32_t prefill_ms;  // buffered before playback starts
    uint32_t capacity_ms; // held at most; older frames drop on overflow
    uint32_t ramp_ms;     // the linear ramp on every edge
} tas_jbuf_config_t;

typedef struct {
    uint32_t played_ms;    // the stream position reached: lost frames count, pauses don't
    uint32_t lost;         // frames never played: concealed as silence, or skipped
    uint32_t underruns;    // times playback paused on an empty buffer
    uint32_t underrun_ms;  // total time paused
    uint32_t short_frames; // short frames dropped because they weren't the final one
} tas_jbuf_stats_t;

typedef enum {
    TAS_JBUF_PREFILL,  // not started yet: silence
    TAS_JBUF_PLAY,     // a frame of the stream
    TAS_JBUF_LOST,     // a missing frame, concealed as silence
    TAS_JBUF_UNDERRUN, // paused on an empty buffer: silence, and the position holds
    TAS_JBUF_DONE,     // played through the drain's last_seq: silence
} tas_jbuf_out_t;

typedef struct {
    int16_t pcm[TAS_JBUF_MAX_SAMPLES];
    uint32_t seq[TAS_JBUF_MAX_SLOTS];
    uint16_t len[TAS_JBUF_MAX_SLOTS]; // samples held; 0 = empty
    uint32_t frame_samples, slots, prefill_frames, ramp, frame_ms, rate;
    uint32_t head;      // the next seq to play
    uint32_t last_seq;  // valid once draining
    bool draining, started, paused;
    bool needs_ramp_in; // the next frame played doesn't continue the last one
    int16_t last_out;   // the last sample pulled
    uint64_t position;  // samples
    tas_jbuf_stats_t stats;
} tas_jbuf_t;

// Opens a new stream at seq 0 and clears the stats. False if the config doesn't fit the storage.
bool tas_jbuf_start(tas_jbuf_t *jb, const tas_jbuf_config_t *cfg);

// Takes one frame of `samples` samples. False if it's dropped: empty or longer than a frame, late,
// a duplicate, or past last_seq.
bool tas_jbuf_push(tas_jbuf_t *jb, uint32_t seq, const int16_t *pcm, size_t samples);

// No frames follow last_seq. Starts playback if it's still prefilling.
void tas_jbuf_drain(tas_jbuf_t *jb, uint32_t last_seq);

// Writes the next frame period (frame samples) of playout into `out`.
tas_jbuf_out_t tas_jbuf_pull(tas_jbuf_t *jb, int16_t *out);

tas_jbuf_stats_t tas_jbuf_stats(const tas_jbuf_t *jb);
