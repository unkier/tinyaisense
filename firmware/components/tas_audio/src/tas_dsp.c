#include "tas_dsp.h"

#include <math.h>

#define TAS_PI 3.14159265f

// RBJ cookbook high-pass with Q = 1/sqrt(2): a 2nd-order Butterworth, -3 dB at `hz`.
static void highpass(tas_biquad_t *f, float hz, uint32_t rate) {
    float w0 = 2 * TAS_PI * hz / (float)rate;
    float c = cosf(w0);
    float alpha = sinf(w0) / (2 * 0.70710678f);
    float a0 = 1 + alpha;
    *f = (tas_biquad_t){
        .b0 = (1 + c) / 2 / a0,
        .b1 = -(1 + c) / a0,
        .b2 = (1 + c) / 2 / a0,
        .a1 = -2 * c / a0,
        .a2 = (1 - alpha) / a0,
    };
}

static float step(tas_biquad_t *f, float x) {
    float y = f->b0 * x + f->z1;
    f->z1 = f->b1 * x - f->a1 * y + f->z2;
    f->z2 = f->b2 * x - f->a2 * y;
    return y;
}

static int16_t saturate(float x) {
    if (x >= INT16_MAX) return INT16_MAX;
    if (x <= INT16_MIN) return INT16_MIN;
    return (int16_t)lrintf(x);
}

void tas_mic_cond_start(tas_mic_cond_t *mic, const tas_mic_cond_config_t *cfg) {
    highpass(&mic->highpass, cfg->highpass_hz, cfg->rate);
    mic->gain = powf(10.0f, cfg->gain_db / 20.0f);
    mic->mute = cfg->rate * cfg->warmup_ms / 1000;
}

void tas_mic_cond_run(tas_mic_cond_t *mic, int16_t *pcm, size_t samples) {
    for (size_t i = 0; i < samples; i++) {
        // The filter runs through the warm-up, so it has settled on the mic's DC by the end.
        int16_t y = saturate(step(&mic->highpass, pcm[i]) * mic->gain);
        if (mic->mute) {
            mic->mute--;
            y = 0;
        }
        pcm[i] = y;
    }
}

void tas_spk_cond_start(tas_spk_cond_t *spk, const tas_spk_cond_config_t *cfg) {
    highpass(&spk->lowcut, cfg->lowcut_hz, cfg->rate);
}

void tas_spk_cond_run(tas_spk_cond_t *spk, int16_t *pcm, size_t samples) {
    for (size_t i = 0; i < samples; i++) pcm[i] = saturate(step(&spk->lowcut, pcm[i]));
}
