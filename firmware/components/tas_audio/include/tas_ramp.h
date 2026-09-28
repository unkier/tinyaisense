#pragma once
// Linear ramps on interleaved s16 PCM. Every audible edge gets one (2 ms on the wire's 16 kHz).

#include <stddef.h>
#include <stdint.h>

// Fades the first `ramp` frames to silence and zeroes the rest. Frame i of the ramp is scaled by
// (ramp - 1 - i) / ramp, so the ramp ends on exact silence and no step exceeds 1/ramp of full level.
void tas_ramp_out(int16_t *pcm, size_t frames, size_t channels, size_t ramp);

// Glides the first `ramp` frames in from `from`, the frame played just before (one sample per
// channel), or from silence when `from` is NULL. Frame i of the ramp is
// from * (ramp - i) / ramp + pcm * i / ramp, so a jump between two signals becomes a straight line.
void tas_ramp_in(int16_t *pcm, size_t frames, size_t channels, size_t ramp, const int16_t *from);
