#pragma once
// A mic stream's send queue (docs/protocol/audio.md, "Mic stream"): the mic task pushes each
// frame it captures, numbered from seq 0, and the sender pops them in order. Capture never waits
// on the network: when the queue is full, the oldest frame is dropped and counted. All storage is
// inside the struct, so a static instance needs no malloc. Not thread-safe: the caller serializes.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TAS_MICQ_MAX_SAMPLES 3200 // 200 ms of mono at 16 kHz
#define TAS_MICQ_MAX_SLOTS 20     // 200 ms of 10 ms frames

typedef struct {
    uint32_t rate;        // samples per second, mono
    uint32_t frame_ms;    // the stream's frame duration
    uint32_t capacity_ms; // held at most
} tas_micq_config_t;

typedef struct {
    uint32_t seq;
    uint64_t ts;        // the device clock at its first sample
    const int16_t *pcm; // valid until the next push or start
    size_t samples;
} tas_micq_frame_t;

typedef struct {
    int16_t pcm[TAS_MICQ_MAX_SAMPLES];
    uint64_t ts[TAS_MICQ_MAX_SLOTS];
    uint32_t frame_samples, slots;
    uint32_t next_seq; // the next frame pushed gets it
    uint32_t count;    // frames queued: seqs next_seq - count to next_seq - 1
    uint32_t dropped;  // frames dropped on overflow
} tas_micq_t;

// Opens a new stream at seq 0, with nothing queued or dropped. False if the config doesn't fit
// the storage.
bool tas_micq_start(tas_micq_t *q, const tas_micq_config_t *cfg);

// Queues the next frame, captured at device time `ts`, dropping the oldest if the queue is full.
void tas_micq_push(tas_micq_t *q, const int16_t *pcm, uint64_t ts);

// Takes the oldest frame queued. False if there's none.
bool tas_micq_pop(tas_micq_t *q, tas_micq_frame_t *frame);

// The seq of the newest frame pushed, popped or not. False if none was.
bool tas_micq_last_seq(const tas_micq_t *q, uint32_t *seq);
