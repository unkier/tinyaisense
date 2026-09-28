#pragma once
// The speaker cut: patching the I2S TX DMA ring in place, so the speaker goes silent within one
// descriptor plus a ramp (5 + 2 ms on the Echo) without stopping the channel, which clicks.
//
// The ring's buffers aren't public, so the planner learns them from on_sent, which reports each
// descriptor as it finishes, in ring order. A cut then leaves the descriptor playing alone, ramps
// the head of the next one down to silence, and zeroes the rest of the ring. The channel must run
// with auto_clear_after_cb, so a descriptor nobody refills plays silence on the next lap.
//
// on_sent runs in the I2S ISR. With CONFIG_I2S_ISR_IRAM_SAFE the speaker module must also put
// tas_cut_sent in IRAM (a linker fragment); this file makes no IDF calls to do it itself. A patch
// landing in the few µs between the DMA moving on and its on_sent silences the descriptor already
// playing, which may click.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TAS_CUT_MAX_DESCS 8

typedef struct {
    int16_t *ring[TAS_CUT_MAX_DESCS]; // the descriptors' buffers, in the order the DMA plays them
    size_t descs, frames, channels, ramp;
    volatile size_t known; // buffers learned so far; written by on_sent
    volatile size_t last;  // ring index of the descriptor that finished last; written by on_sent
} tas_cut_t;

// A ring of `descs` descriptors, each `frames` frames of interleaved s16 with `channels` channels.
// The cut ramps over `ramp` frames (1..frames). False if the ring doesn't fit.
bool tas_cut_init(tas_cut_t *cut, size_t descs, size_t frames, size_t channels, size_t ramp);

// From the on_sent callback: `dma_buf` just finished playing. ISR-safe: no locks, no calls.
void tas_cut_sent(tas_cut_t *cut, void *dma_buf);

// Silences the ring after the descriptor now playing. False while the ring isn't known yet (the
// first lap after the channel started); the caller then falls back to disabling the channel.
bool tas_cut_patch(tas_cut_t *cut);
