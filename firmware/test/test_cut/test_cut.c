#include <stdlib.h>
#include <string.h>
#include <unity.h>

#include "tas_cut.h"

// The Echo's ring: 8 descriptors of 2.5 ms, stereo L=R at 16 kHz, cut with a 2 ms ramp. The DMA
// may fetch up to 0.5 ms of the next descriptor before on_sent reports the one playing done.
#define RATE 16000
#define DESCS 8
#define FRAMES 40
#define CH 2
#define RAMP 32
#define LEAD 8
#define LEVEL 30000
#define FRAME_US 62.5
#define PERIOD_US 2500
#define T0 1000000 // when the channel started

static const tas_cut_config_t config = {.descs = DESCS,
                                        .frames = FRAMES,
                                        .channels = CH,
                                        .rate = RATE,
                                        .ramp = RAMP,
                                        .fetch_lead = LEAD};
static int16_t bufs[DESCS][FRAMES * CH];
static tas_cut_t cut;
static int64_t sent_at; // when the last on_sent ran
static uint32_t sents;

void setUp(void) {
    memset(bufs, 0, sizeof bufs);
    TEST_ASSERT_TRUE(tas_cut_init(&cut, &config));
    sent_at = T0;
    sents = 0;
}

void tearDown(void) {}

// The DMA finishes `n` more descriptors, one period apart.
static void play_descriptors(size_t n) {
    for (size_t i = 0; i < n; i++) {
        sent_at += PERIOD_US;
        tas_cut_sent(&cut, bufs[sents++ % DESCS], sent_at);
    }
}

// `frames` frames after the last on_sent.
static int64_t after(double frames) {
    return sent_at + (int64_t)(frames * FRAME_US);
}

// Loud audio in every descriptor: a triangle wave, two to a lap, so an unramped edge anywhere jumps
// by far more than the wave's own slope of 750 per frame.
static void fill_ring(void) {
    for (size_t d = 0; d < DESCS; d++) {
        for (size_t f = 0; f < FRAMES; f++) {
            size_t t = (d * FRAMES + f) % 160;
            int16_t x = (int16_t)(t < 80 ? -LEVEL + 750 * (int)t : LEVEL - 750 * (int)(t - 80));
            bufs[d][f * CH] = bufs[d][f * CH + 1] = x;
        }
    }
}

// What the speaker plays over the next lap, from the descriptor playing now.
static void listen_lap(size_t playing, int16_t *heard) {
    for (size_t i = 0; i < DESCS; i++) {
        memcpy(heard + i * FRAMES * CH, bufs[(playing + i) % DESCS], sizeof bufs[0]);
    }
}

static int max_step(const int16_t *pcm, size_t samples) {
    int worst = 0;
    for (size_t i = CH; i < samples; i++) {
        int step = abs(pcm[i] - pcm[i - CH]);
        if (step > worst) worst = step;
    }
    return worst;
}

// Cuts `played` frames into the descriptor playing, and checks what the next lap sounds like:
// the first `kept` frames are the audio the DMA already has, then a ramp, then silence.
static void check_cut(double played, size_t kept) {
    size_t playing = sents % DESCS;
    fill_ring();
    int16_t before[DESCS * FRAMES * CH];
    listen_lap(playing, before);

    TEST_ASSERT_TRUE(tas_cut_patch(&cut, after(played)));

    int16_t heard[DESCS * FRAMES * CH];
    listen_lap(playing, heard);
    TEST_ASSERT_EQUAL_MEMORY(before, heard, kept * CH * sizeof *heard);
    TEST_ASSERT_LESS_OR_EQUAL_INT(LEVEL / RAMP + 750, max_step(heard, DESCS * FRAMES * CH));
    for (size_t i = (kept + RAMP) * CH; i < DESCS * FRAMES * CH; i++) {
        TEST_ASSERT_EQUAL_INT16(0, heard[i]);
    }
}

static void test_no_cut_before_the_ring_is_known(void) {
    play_descriptors(DESCS - 1);
    fill_ring();
    int16_t before[DESCS][FRAMES * CH];
    memcpy(before, bufs, sizeof bufs);

    TEST_ASSERT_FALSE(tas_cut_patch(&cut, after(1)));
    TEST_ASSERT_EQUAL_MEMORY(before, bufs, sizeof bufs);
}

static void test_a_cut_at_any_dma_phase_ramps_the_next_descriptor(void) {
    for (size_t phase = 0; phase < DESCS; phase++) {
        for (size_t played = 0; played + LEAD < FRAMES; played += 7) {
            setUp();
            play_descriptors(DESCS + phase);
            check_cut((double)played, FRAMES);
        }
    }
}

// The DMA fetches ahead of what plays: late in a descriptor, the head of the next may be in.
static void test_a_late_cut_ramps_after_what_the_dma_may_have_fetched(void) {
    play_descriptors(DESCS + 3);
    check_cut(FRAMES - 3, FRAMES + LEAD - 3);
}

// on_sent can run late, e.g. behind other interrupts: by then the DMA is in the next descriptor.
static void test_a_cut_past_an_on_sent_that_is_late_spares_the_descriptor_the_dma_is_in(void) {
    play_descriptors(DESCS + 2);
    check_cut(FRAMES + 5, 2 * FRAMES);
}

static void test_a_cut_tells_when_its_ramp_ends(void) {
    play_descriptors(DESCS + 2);
    int64_t at;
    TEST_ASSERT_TRUE(tas_cut_patch(&cut, after(10)));
    TEST_ASSERT_FALSE(tas_cut_silent_at(&cut, &at)); // the DMA isn't at the ramp yet

    play_descriptors(1);
    TEST_ASSERT_TRUE(tas_cut_silent_at(&cut, &at));
    TEST_ASSERT_EQUAL_INT64(sent_at + RAMP * FRAME_US, at);

    play_descriptors(1);
    TEST_ASSERT_TRUE(tas_cut_silent_at(&cut, &at));
    TEST_ASSERT_EQUAL_INT64(sent_at - PERIOD_US + RAMP * FRAME_US, at);
}

static void test_a_late_cut_ends_its_ramp_after_the_fetched_frames(void) {
    play_descriptors(DESCS + 2);
    int64_t at;
    TEST_ASSERT_TRUE(tas_cut_patch(&cut, after(FRAMES - 3)));

    play_descriptors(1);
    TEST_ASSERT_TRUE(tas_cut_silent_at(&cut, &at));
    TEST_ASSERT_EQUAL_INT64(sent_at + (int64_t)((LEAD - 3 + RAMP) * FRAME_US), at);
}

// on_sent wakes the speaker's task when the DMA moves onto the ramp, to report the cut's timing.
static void test_on_sent_tells_when_the_dma_reaches_the_ramp(void) {
    play_descriptors(DESCS + 2);
    TEST_ASSERT_TRUE(tas_cut_patch(&cut, after(FRAMES + 5))); // the ramp is two descriptors on

    TEST_ASSERT_FALSE(tas_cut_sent(&cut, bufs[2], after(FRAMES)));
    TEST_ASSERT_TRUE(tas_cut_sent(&cut, bufs[3], after(FRAMES)));
    TEST_ASSERT_FALSE(tas_cut_sent(&cut, bufs[4], after(FRAMES)));
}

// A re-enabled channel plays its ring from the first descriptor again, wherever it stopped.
static void test_after_a_restart_a_cut_spares_the_first_descriptor(void) {
    play_descriptors(DESCS + 3);
    sent_at += 12345;
    tas_cut_restart(&cut, sent_at);
    sents = 0; // the next on_sent is the first descriptor's

    check_cut(5, FRAMES);
    int64_t at;
    play_descriptors(1);
    TEST_ASSERT_TRUE(tas_cut_silent_at(&cut, &at));
    TEST_ASSERT_EQUAL_INT64(sent_at + RAMP * FRAME_US, at);
}

// The I2S ISR reports only the last descriptor that finished, so one that runs late skips
// on_sents. The cut still learns that the DMA got to its ramp, and when at the latest.
static void test_skipped_on_sents_still_reach_the_ramp(void) {
    play_descriptors(DESCS + 2);
    TEST_ASSERT_TRUE(tas_cut_patch(&cut, after(FRAMES + 5))); // the ramp is two descriptors on

    sent_at += 3 * PERIOD_US; // bufs[2] and bufs[3] finished unreported
    TEST_ASSERT_TRUE(tas_cut_sent(&cut, bufs[4], sent_at));
    int64_t at;
    TEST_ASSERT_TRUE(tas_cut_silent_at(&cut, &at));
    TEST_ASSERT_EQUAL_INT64(sent_at + RAMP * FRAME_US, at);

    sent_at += 2 * PERIOD_US;
    TEST_ASSERT_FALSE(tas_cut_sent(&cut, bufs[6], sent_at));
    TEST_ASSERT_TRUE(tas_cut_silent_at(&cut, &at));
    TEST_ASSERT_EQUAL_INT64(sent_at - 2 * PERIOD_US + RAMP * FRAME_US, at);
}

static void test_a_ring_that_does_not_fit_is_refused(void) {
    tas_cut_config_t bad = config;
    bad.descs = TAS_CUT_MAX_DESCS + 1;
    TEST_ASSERT_FALSE(tas_cut_init(&cut, &bad));
    bad = config;
    bad.descs = 1; // no descriptor to ramp but the one playing
    TEST_ASSERT_FALSE(tas_cut_init(&cut, &bad));
    bad = config;
    bad.ramp = 0;
    TEST_ASSERT_FALSE(tas_cut_init(&cut, &bad));
    bad = config;
    bad.fetch_lead = FRAMES - RAMP + 1; // a late cut's ramp wouldn't fit in the next descriptor
    TEST_ASSERT_FALSE(tas_cut_init(&cut, &bad));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_no_cut_before_the_ring_is_known);
    RUN_TEST(test_a_cut_at_any_dma_phase_ramps_the_next_descriptor);
    RUN_TEST(test_a_late_cut_ramps_after_what_the_dma_may_have_fetched);
    RUN_TEST(test_a_cut_past_an_on_sent_that_is_late_spares_the_descriptor_the_dma_is_in);
    RUN_TEST(test_a_cut_tells_when_its_ramp_ends);
    RUN_TEST(test_a_late_cut_ends_its_ramp_after_the_fetched_frames);
    RUN_TEST(test_on_sent_tells_when_the_dma_reaches_the_ramp);
    RUN_TEST(test_after_a_restart_a_cut_spares_the_first_descriptor);
    RUN_TEST(test_skipped_on_sents_still_reach_the_ramp);
    RUN_TEST(test_a_ring_that_does_not_fit_is_refused);
    return UNITY_END();
}
