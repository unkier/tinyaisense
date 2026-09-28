#include <stdlib.h>
#include <string.h>
#include <unity.h>

#include "tas_cut.h"

// The Echo's ring: 6 descriptors of 5 ms, stereo L=R at 16 kHz, cut with a 2 ms ramp.
#define DESCS 6
#define FRAMES 80
#define CH 2
#define RAMP 32
#define LEVEL 30000

static int16_t bufs[DESCS][FRAMES * CH];
static tas_cut_t cut;

void setUp(void) {
    memset(bufs, 0, sizeof bufs);
    TEST_ASSERT_TRUE(tas_cut_init(&cut, DESCS, FRAMES, CH, RAMP));
}

void tearDown(void) {}

// The DMA finishes `n` descriptors, starting from buffer 0.
static void play_descriptors(size_t n) {
    for (size_t i = 0; i < n; i++) tas_cut_sent(&cut, bufs[i % DESCS]);
}

// Loud audio in every descriptor: a triangle wave, so an unramped edge anywhere jumps by far more
// than the wave's own slope of 1000 per frame.
static void fill_ring(void) {
    for (size_t d = 0; d < DESCS; d++) {
        for (size_t f = 0; f < FRAMES; f++) {
            size_t t = (d * FRAMES + f) % 120;
            int16_t x = (int16_t)(t < 60 ? -LEVEL + 1000 * (int)t : LEVEL - 1000 * (int)(t - 60));
            bufs[d][f * CH] = bufs[d][f * CH + 1] = x;
        }
    }
}

static void test_no_cut_before_the_ring_is_known(void) {
    play_descriptors(DESCS - 1);
    fill_ring();
    int16_t before[DESCS][FRAMES * CH];
    memcpy(before, bufs, sizeof bufs);

    TEST_ASSERT_FALSE(tas_cut_patch(&cut));
    TEST_ASSERT_EQUAL_MEMORY(before, bufs, sizeof bufs);
}

// What the speaker plays over the next lap, from the descriptor playing now.
static void listen_lap(size_t playing, int16_t *heard) {
    for (size_t i = 0; i < DESCS; i++) {
        memcpy(heard + i * FRAMES * CH, bufs[(playing + i) % DESCS], sizeof bufs[0]);
    }
}

static void test_a_cut_at_any_dma_phase_is_silent_within_a_descriptor_and_a_ramp(void) {
    for (size_t phase = 0; phase < DESCS; phase++) {
        setUp();
        play_descriptors(DESCS + phase + 1); // a lap, then up to buffer `phase` finished
        size_t playing = (phase + 1) % DESCS;
        fill_ring();
        int16_t unpatched[FRAMES * CH];
        memcpy(unpatched, bufs[playing], sizeof unpatched);

        TEST_ASSERT_TRUE(tas_cut_patch(&cut));

        int16_t heard[DESCS * FRAMES * CH];
        listen_lap(playing, heard);
        TEST_ASSERT_EQUAL_MEMORY(unpatched, heard, sizeof unpatched); // the DMA owns this one
        int worst = 0;
        for (size_t i = CH; i < DESCS * FRAMES * CH; i++) {
            int step = abs(heard[i] - heard[i - CH]);
            if (step > worst) worst = step;
        }
        TEST_ASSERT_LESS_OR_EQUAL_INT(LEVEL / RAMP + 1000, worst);
        for (size_t i = (FRAMES + RAMP) * CH; i < DESCS * FRAMES * CH; i++) {
            TEST_ASSERT_EQUAL_INT16(0, heard[i]);
        }
    }
}

static void test_a_ring_that_does_not_fit_is_refused(void) {
    TEST_ASSERT_FALSE(tas_cut_init(&cut, TAS_CUT_MAX_DESCS + 1, FRAMES, CH, RAMP));
    TEST_ASSERT_FALSE(tas_cut_init(&cut, 1, FRAMES, CH, RAMP)); // nothing after the one playing
    TEST_ASSERT_FALSE(tas_cut_init(&cut, DESCS, FRAMES, CH, 0));
    TEST_ASSERT_FALSE(tas_cut_init(&cut, DESCS, FRAMES, CH, FRAMES + 1));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_no_cut_before_the_ring_is_known);
    RUN_TEST(test_a_cut_at_any_dma_phase_is_silent_within_a_descriptor_and_a_ramp);
    RUN_TEST(test_a_ring_that_does_not_fit_is_refused);
    return UNITY_END();
}
