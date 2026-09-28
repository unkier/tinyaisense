#include <stdlib.h>
#include <unity.h>

#include "tas_ramp.h"

#define RAMP 32 // 2 ms at 16 kHz

void setUp(void) {}
void tearDown(void) {}

static int max_step(const int16_t *pcm, size_t n, size_t channels) {
    int worst = 0;
    for (size_t i = channels; i < n * channels; i++) {
        int step = abs(pcm[i] - pcm[i - channels]);
        if (step > worst) worst = step;
    }
    return worst;
}

static void test_ramp_out_fades_full_scale_to_silence_without_a_step(void) {
    int16_t pcm[1 + 80];
    for (size_t i = 0; i < 81; i++) pcm[i] = INT16_MAX;

    tas_ramp_out(pcm + 1, 80, 1, RAMP); // pcm[0] is the unramped sample played before

    TEST_ASSERT_LESS_OR_EQUAL_INT(INT16_MAX / RAMP + 1, max_step(pcm, 81, 1));
    for (size_t i = 1 + RAMP; i < 81; i++) TEST_ASSERT_EQUAL_INT16(0, pcm[i]);
    TEST_ASSERT_EQUAL_INT16(0, pcm[RAMP]); // the ramp's own last frame is already silent
}

static void test_ramp_in_fades_up_from_silence_without_a_step(void) {
    int16_t pcm[1 + 80];
    pcm[0] = 0; // the silence played before
    for (size_t i = 1; i < 81; i++) pcm[i] = INT16_MIN;

    tas_ramp_in(pcm + 1, 80, 1, RAMP, NULL);

    TEST_ASSERT_LESS_OR_EQUAL_INT(-(INT16_MIN / RAMP) + 1, max_step(pcm, 81, 1));
    for (size_t i = 1 + RAMP; i < 81; i++) TEST_ASSERT_EQUAL_INT16(INT16_MIN, pcm[i]);
}

static void test_ramp_in_glides_from_the_last_sample_played(void) {
    // Stereo: the left channel jumps from full negative to full positive, the right one is steady.
    int16_t pcm[2 * (1 + 80)];
    pcm[0] = -INT16_MAX;
    pcm[1] = 1000;
    for (size_t i = 1; i < 81; i++) {
        pcm[2 * i] = INT16_MAX;
        pcm[2 * i + 1] = 1000;
    }

    tas_ramp_in(pcm + 2, 80, 2, RAMP, pcm);

    TEST_ASSERT_LESS_OR_EQUAL_INT(2 * INT16_MAX / RAMP + 1, max_step(pcm, 81, 2));
    for (size_t i = 1; i < 81; i++) TEST_ASSERT_EQUAL_INT16(1000, pcm[2 * i + 1]);
    TEST_ASSERT_EQUAL_INT16(INT16_MAX, pcm[2 * (1 + RAMP)]);
}

static void test_ramp_out_applies_one_gain_per_frame_across_channels(void) {
    int16_t pcm[2 * 40];
    for (size_t i = 0; i < 40; i++) {
        pcm[2 * i] = 3200;
        pcm[2 * i + 1] = -3200;
    }

    tas_ramp_out(pcm, 40, 2, RAMP);

    for (size_t i = 0; i < 40; i++) TEST_ASSERT_EQUAL_INT16(-pcm[2 * i], pcm[2 * i + 1]);
    TEST_ASSERT_EQUAL_INT16(3100, pcm[0]); // 3200 * 31/32
    TEST_ASSERT_EQUAL_INT16(0, pcm[2 * (RAMP - 1)]);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_ramp_out_fades_full_scale_to_silence_without_a_step);
    RUN_TEST(test_ramp_in_fades_up_from_silence_without_a_step);
    RUN_TEST(test_ramp_in_glides_from_the_last_sample_played);
    RUN_TEST(test_ramp_out_applies_one_gain_per_frame_across_channels);
    return UNITY_END();
}
