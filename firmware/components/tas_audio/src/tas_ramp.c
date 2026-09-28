#include "tas_ramp.h"

static int16_t scale(int16_t x, size_t num, size_t den) {
    return (int16_t)((int32_t)x * (int32_t)num / (int32_t)den);
}

void tas_ramp_out(int16_t *pcm, size_t frames, size_t channels, size_t ramp) {
    for (size_t f = 0; f < frames; f++) {
        size_t gain = f < ramp ? ramp - 1 - f : 0;
        for (size_t c = 0; c < channels; c++) {
            pcm[f * channels + c] = scale(pcm[f * channels + c], gain, ramp);
        }
    }
}

void tas_ramp_in(int16_t *pcm, size_t frames, size_t channels, size_t ramp, const int16_t *from) {
    for (size_t f = 0; f < frames && f < ramp; f++) {
        for (size_t c = 0; c < channels; c++) {
            int32_t start = from ? from[c] : 0;
            int32_t x = pcm[f * channels + c];
            int32_t mix = start * (int32_t)(ramp - f) + x * (int32_t)f;
            pcm[f * channels + c] = (int16_t)(mix / (int32_t)ramp);
        }
    }
}
