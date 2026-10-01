#pragma once
// The speaker cut: patching the I2S TX DMA ring in place, so the speaker goes silent within about
// one descriptor plus a ramp without stopping the channel, which clicks.
//
// The ring's buffers aren't public, so the planner learns them from on_sent, which reports each
// descriptor as it finishes, in ring order. A cut leaves alone what the DMA may already have, ramps
// down to silence after it, and zeroes the rest of the ring. The channel must run with
// auto_clear_after_cb, so a descriptor nobody refills plays silence on the next lap.
//
// What the DMA may already have is timed from the last on_sent: the descriptor playing, plus the
// `fetch_lead` frames of the next one it may have fetched ahead. A cut late in a descriptor ramps
// after those, and one past an on_sent that runs late spares the descriptor the DMA has moved into.
// So a patch must go in right after it's planned, with nothing preempting it.
//
// on_sent runs in the I2S ISR, maybe on the other core: it is the only writer of the ring and the
// times, which the planner reads through a sequence count. An ISR that runs late reports only the
// last descriptor that finished; the planner counts the ones it skipped. With
// CONFIG_I2S_ISR_IRAM_SAFE the speaker module must also put tas_cut_sent in IRAM (a linker
// fragment); this file makes no IDF calls to do it itself. The other calls must not run
// concurrently with each other.

#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TAS_CUT_MAX_DESCS 8

typedef struct {
    size_t descs;    // in the ring: 2..TAS_CUT_MAX_DESCS
    size_t frames;   // per descriptor, of interleaved s16
    size_t channels; // per frame
    uint32_t rate;   // frames per second
    size_t ramp;     // frames the cut ramps over, at least 1
    size_t fetch_lead; // frames of the next descriptor the DMA may fetch before on_sent
                       // (fetch_lead + ramp <= frames)
} tas_cut_config_t;

typedef struct {
    tas_cut_config_t cfg;
    // Written by on_sent, inside odd values of seq. Not _Atomic: a 64-bit atomic would take a lock
    // in the ISR. The planner rereads while seq is odd or has moved on.
    _Atomic uint32_t seq;
    int16_t *volatile ring[TAS_CUT_MAX_DESCS]; // the descriptors' buffers, in the order they play
    volatile size_t known;                     // buffers learned so far
    volatile size_t last;                      // ring index of the descriptor that finished last
    volatile uint32_t sents;                   // descriptors finished so far, wrapping
    volatile int64_t sent_us[TAS_CUT_MAX_DESCS]; // when each last finished, at the latest
    // The cut in flight, written by the planner.
    _Atomic bool cutting;
    _Atomic uint32_t reach; // the on_sent that moves the DMA onto the ramped descriptor
    size_t ramped, offset;  // that descriptor, and the frames it plays before the ramp
} tas_cut_t;

// False if the config is out of range.
bool tas_cut_init(tas_cut_t *cut, const tas_cut_config_t *cfg);

// From the on_sent callback at `now_us`: `dma_buf` just finished playing. True if that moved the
// DMA onto the ramped descriptor of a cut, so the caller can report when it went silent.
// ISR-safe: no locks, no calls.
bool tas_cut_sent(tas_cut_t *cut, void *dma_buf, int64_t now_us);

// The channel was started again at `now_us`, so the DMA plays the ring from its first descriptor;
// a cut in flight is dropped. The ring must have been learned from a channel that started there
// too, and on_sent mustn't run concurrently: the channel is stopped.
void tas_cut_restart(tas_cut_t *cut, int64_t now_us);

// Silences the ring after what the DMA may have, called at `now_us`. False while the ring isn't
// known yet (the first lap after the channel started) or on_sent is a whole ring behind; the
// caller then falls back to stopping the channel.
bool tas_cut_patch(tas_cut_t *cut, int64_t now_us);

// When the last cut's ramp ends: false until the DMA reaches the ramped descriptor, and again a
// lap after that.
bool tas_cut_silent_at(const tas_cut_t *cut, int64_t *at_us);
