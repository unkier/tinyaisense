#include <math.h>
#include <stdlib.h>
#include <unity.h>

#include "tas_dsp.h"

#define RATE 16000
#define PI 3.14159265358979

static const tas_mic_cond_config_t MIC = {
    .rate = RATE, .highpass_hz = 100, .gain_db = 15, .warmup_ms = 20};

static tas_mic_cond_t mic;
static int16_t pcm[RATE]; // 1 s

void setUp(void) {}
void tearDown(void) {}

static void tone(double hz, double amplitude, size_t samples) {
    for (size_t i = 0; i < samples; i++) {
        pcm[i] = (int16_t)lround(amplitude * sin(2 * PI * hz * (double)i / RATE));
    }
}

static void test_the_mic_is_silent_for_20_ms_after_each_enable(void) {
    for (int enable = 0; enable < 2; enable++) {
        tone(1000, 1000, 480);
        tas_mic_cond_start(&mic, &MIC);
        tas_mic_cond_run(&mic, pcm, 200); // warm-up spans calls
        tas_mic_cond_run(&mic, pcm + 200, 280);

        for (size_t i = 0; i < 320; i++) TEST_ASSERT_EQUAL_INT16(0, pcm[i]);
        int loudest = 0;
        for (size_t i = 320; i < 480; i++) loudest = abs(pcm[i]) > loudest ? abs(pcm[i]) : loudest;
        TEST_ASSERT_GREATER_THAN_INT(1000, loudest);
    }
}

// The amplitude of a steady sine: RMS times sqrt(2), over the last half second.
static double amplitude(void) {
    double sum = 0;
    for (size_t i = RATE / 2; i < RATE; i++) sum += (double)pcm[i] * pcm[i];
    return sqrt(2 * sum / (RATE / 2));
}

static double mic_amplitude(double hz, double level) {
    tone(hz, level, RATE);
    tas_mic_cond_start(&mic, &MIC);
    tas_mic_cond_run(&mic, pcm, RATE);
    return amplitude();
}

static void test_the_mic_gains_speech_by_15_db(void) {
    TEST_ASSERT_DOUBLE_WITHIN(0.02 * 5623, 5623, mic_amplitude(1000, 1000)); // 10^(15/20) = 5.623
}

// A 2nd-order Butterworth high-pass passes |H(f)| = 1 / sqrt(1 + (fc/f)^4).
static void test_the_mic_high_pass_cuts_below_100_hz(void) {
    TEST_ASSERT_DOUBLE_WITHIN(0.03 * 1364, 1364, mic_amplitude(50, 1000)); // 5623 * 0.2425
    TEST_ASSERT_DOUBLE_WITHIN(0.03 * 3976, 3976, mic_amplitude(100, 1000)); // -3 dB
}

static void test_the_mic_removes_dc(void) {
    for (size_t i = 0; i < RATE; i++) pcm[i] = 2000;
    tas_mic_cond_start(&mic, &MIC);
    tas_mic_cond_run(&mic, pcm, RATE);

    for (size_t i = RATE / 10; i < RATE; i++) TEST_ASSERT_INT16_WITHIN(1, 0, pcm[i]);
}

static void test_loud_input_saturates_instead_of_wrapping(void) {
    tone(1000, 30000, RATE);
    int16_t in[RATE];
    for (size_t i = 0; i < RATE; i++) in[i] = pcm[i];
    tas_mic_cond_start(&mic, &MIC);
    tas_mic_cond_run(&mic, pcm, RATE);

    int16_t hi = 0, lo = 0;
    for (size_t i = RATE / 2; i < RATE; i++) {
        if (in[i] > 3000) TEST_ASSERT_GREATER_THAN_INT16(0, pcm[i]);
        if (in[i] < -3000) TEST_ASSERT_LESS_THAN_INT16(0, pcm[i]);
        hi = pcm[i] > hi ? pcm[i] : hi;
        lo = pcm[i] < lo ? pcm[i] : lo;
    }
    TEST_ASSERT_EQUAL_INT16(INT16_MAX, hi);
    TEST_ASSERT_EQUAL_INT16(INT16_MIN, lo);
}

static const tas_spk_cond_config_t SPK = {.rate = RATE, .lowcut_hz = 300};

static double speaker_amplitude(double hz, double level) {
    tas_spk_cond_t spk;
    tone(hz, level, RATE);
    tas_spk_cond_start(&spk, &SPK);
    tas_spk_cond_run(&spk, pcm, RATE);
    return amplitude();
}

static void test_the_speaker_low_cut_is_300_hz(void) {
    TEST_ASSERT_DOUBLE_WITHIN(0.02 * 9959, 9959, speaker_amplitude(1000, 10000)); // unity gain
    TEST_ASSERT_DOUBLE_WITHIN(0.03 * 7071, 7071, speaker_amplitude(300, 10000));  // -3 dB
    TEST_ASSERT_DOUBLE_WITHIN(0.05 * 1104, 1104, speaker_amplitude(100, 10000));  // -19 dB
}

static void test_volume_0_mutes_1_is_minus_40_db_and_100_is_unity(void) {
    TEST_ASSERT_EQUAL_FLOAT(0.0f, tas_volume_gain(0));
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 0.01f, tas_volume_gain(1));
    TEST_ASSERT_EQUAL_FLOAT(1.0f, tas_volume_gain(100));
}

static void test_volume_steps_evenly_in_db_between(void) {
    // -40 dB * 50/99 and * 20/99, as the Simulator's curve gives them.
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 0.0977010f, tas_volume_gain(50));
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 0.3944206f, tas_volume_gain(80));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_the_mic_is_silent_for_20_ms_after_each_enable);
    RUN_TEST(test_the_mic_gains_speech_by_15_db);
    RUN_TEST(test_the_mic_high_pass_cuts_below_100_hz);
    RUN_TEST(test_the_mic_removes_dc);
    RUN_TEST(test_loud_input_saturates_instead_of_wrapping);
    RUN_TEST(test_the_speaker_low_cut_is_300_hz);
    RUN_TEST(test_volume_0_mutes_1_is_minus_40_db_and_100_is_unity);
    RUN_TEST(test_volume_steps_evenly_in_db_between);
    return UNITY_END();
}
