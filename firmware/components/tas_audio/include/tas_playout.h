#pragma once
// What of a speaker stream has left the DMA (docs/protocol/audio.md, "Speaker stream"). The jitter
// buffer's stats are pull-side, but stop and drained report what was heard; and drained goes out
// only once the last sample has left the DMA.
//
// The speaker writes playout into the I2S ring, which loops through `descs` descriptors. The
// driver keeps a queue of free ones: on_sent adds the descriptor that just finished, dropping the
// oldest once descs - 1 are queued, and each write fills the oldest. So a descriptor written
// plays out a lap after it was freed. The tracker mirrors that queue with the sent count at which
// each descriptor was freed, which tells it when each write will have played.
//
// tas_playout_sent runs in the I2S ISR: the caller makes every call one critical section.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tas_jbuf.h"

#define TAS_PLAYOUT_MAX_DESCS 8
#define TAS_PLAYOUT_MAX_FRAMES (TAS_PLAYOUT_MAX_DESCS + 1) // a frame is a descriptor at least

typedef struct {
    uint32_t done_at; // the sent count once its last descriptor has played
    tas_jbuf_stats_t stats;
} tas_playout_frame_t;

typedef struct {
    size_t descs;
    uint32_t sents; // descriptors the DMA has finished, wrapping
    // The driver's free queue: when each descriptor in it was freed, oldest first.
    uint32_t freed[TAS_PLAYOUT_MAX_DESCS];
    size_t freed_first, freed_count;
    // Frames written but not heard yet, oldest first.
    tas_playout_frame_t frames[TAS_PLAYOUT_MAX_FRAMES];
    size_t frames_first, frames_count;
    uint32_t written_done_at; // when the stream's last descriptor written will have played
    tas_jbuf_stats_t heard;
} tas_playout_t;

// The channel started on a ring the driver preloaded: none of it is free. False if `descs` is
// out of range (2..TAS_PLAYOUT_MAX_DESCS).
bool tas_playout_init(tas_playout_t *p, size_t descs);

// The channel was stopped and started again on a preloaded ring.
void tas_playout_restart(tas_playout_t *p);

// From on_sent: the DMA has finished `sents` descriptors so far, and the last of them is free.
void tas_playout_sent(tas_playout_t *p, uint32_t sents);

// A descriptor of playout was written into the oldest free one.
void tas_playout_wrote(tas_playout_t *p);

// The descriptors written so far end a frame of playout: the stream's stats are `stats` once it
// has played.
void tas_playout_frame(tas_playout_t *p, const tas_jbuf_stats_t *stats);

// A new stream: nothing of it heard yet, and its frames are its own.
void tas_playout_stream(tas_playout_t *p);

// The stats of the stream's frames that have left the DMA.
tas_jbuf_stats_t tas_playout_heard(tas_playout_t *p);

// Whether every descriptor of the stream written so far has left the DMA.
bool tas_playout_idle(const tas_playout_t *p);
