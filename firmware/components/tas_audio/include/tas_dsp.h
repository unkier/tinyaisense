#pragma once
// Audio conditioning on the device (docs/protocol/audio.md, "Conditioning"), on mono s16 PCM in
// place. The hub does none, so the wire carries clean, speech-level PCM.

#include <stddef.h>
#include <stdint.h>

// A 2nd-order section: transposed direct form II, in float (the ESP32 has an FPU).
typedef struct {
    float b0, b1, b2, a1, a2;
    float z1, z2;
} tas_biquad_t;

typedef struct {
    uint32_t rate;      // samples per second
    float highpass_hz;  // 2nd-order Butterworth high-pass corner
    float gain_db;      // saturating gain after the filter
    uint32_t warmup_ms; // silence after the mic is enabled
} tas_mic_cond_config_t;

typedef struct {
    tas_biquad_t highpass;
    float gain;
    uint32_t mute; // samples still to silence
} tas_mic_cond_t;

// Call on every mic enable: it clears the filter and restarts the warm-up silence.
void tas_mic_cond_start(tas_mic_cond_t *mic, const tas_mic_cond_config_t *cfg);
void tas_mic_cond_run(tas_mic_cond_t *mic, int16_t *pcm, size_t samples);

typedef struct {
    uint32_t rate;    // samples per second
    float lowcut_hz;  // 2nd-order Butterworth high-pass corner
} tas_spk_cond_config_t;

typedef struct {
    tas_biquad_t lowcut;
} tas_spk_cond_t;

// Call on every speaker stream start: it clears the filter.
void tas_spk_cond_start(tas_spk_cond_t *spk, const tas_spk_cond_config_t *cfg);
void tas_spk_cond_run(tas_spk_cond_t *spk, int16_t *pcm, size_t samples);

// The speaker's software gain at volume `level` (docs/protocol/kinds/speaker.md#set_volume): 0
// mutes, 1 is -40 dB and 100 is 0 dB, evenly spaced in dB between. Above 100 counts as 100.
float tas_volume_gain(uint8_t level);
