#include <math.h>
#include <stdlib.h>
#include <unity.h>

#include "tas_jbuf.h"

#define FRAME 160 // 10 ms at 16 kHz
#define MID (FRAME / 2)

static tas_jbuf_t jb;
static int16_t out[FRAME];

static const tas_jbuf_config_t CONFIG = {
    .rate = 16000, .frame_ms = 10, .prefill_ms = 40, .capacity_ms = 200, .ramp_ms = 2};

// Frame `seq` is a steady level unique to it, so the middle of a pulled frame says which one played.
static int16_t level(uint32_t seq) {
    return (int16_t)(100 * (seq + 1));
}

static void push(uint32_t seq) {
    int16_t pcm[FRAME];
    for (size_t i = 0; i < FRAME; i++) pcm[i] = level(seq);
    TEST_ASSERT_TRUE(tas_jbuf_push(&jb, seq, pcm, FRAME));
}

static void push_range(uint32_t first, uint32_t last) {
    for (uint32_t seq = first; seq <= last; seq++) push(seq);
}

static void expect_play(uint32_t seq) {
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, tas_jbuf_pull(&jb, out));
    TEST_ASSERT_EQUAL_INT16(level(seq), out[MID]);
}

static void expect_silence(tas_jbuf_out_t outcome) {
    TEST_ASSERT_EQUAL_INT(outcome, tas_jbuf_pull(&jb, out));
    for (size_t i = 0; i < FRAME; i++) TEST_ASSERT_EQUAL_INT16(0, out[i]);
}

void setUp(void) {
    TEST_ASSERT_TRUE(tas_jbuf_start(&jb, &CONFIG));
}

void tearDown(void) {}

static void test_playback_waits_for_the_prefill(void) {
    push_range(0, 2);
    expect_silence(TAS_JBUF_PREFILL);

    push(3);
    expect_play(0);
}

static void test_a_gap_with_later_frames_buffered_is_lost_and_skipped(void) {
    push(0);
    push_range(2, 4);

    expect_play(0);
    expect_silence(TAS_JBUF_LOST);
    expect_play(2);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(1, stats.lost);
    TEST_ASSERT_EQUAL_UINT32(30, stats.played_ms);
    TEST_ASSERT_EQUAL_UINT32(0, stats.underruns);
}

static void test_an_empty_buffer_pauses_without_skipping(void) {
    push_range(0, 3);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);

    expect_silence(TAS_JBUF_UNDERRUN);
    expect_silence(TAS_JBUF_UNDERRUN);
    push(4);
    expect_play(4);
    expect_silence(TAS_JBUF_UNDERRUN);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(2, stats.underruns);
    TEST_ASSERT_EQUAL_UINT32(30, stats.underrun_ms);
    TEST_ASSERT_EQUAL_UINT32(50, stats.played_ms);
    TEST_ASSERT_EQUAL_UINT32(0, stats.lost);
}

static void test_after_drain_missing_frames_are_concealed_until_last_seq(void) {
    push_range(0, 3);
    push(5);
    tas_jbuf_drain(&jb, 7);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);

    expect_silence(TAS_JBUF_LOST);
    expect_play(5);
    expect_silence(TAS_JBUF_LOST); // an empty buffer, but no pause after drain
    push(7);
    expect_play(7);
    expect_silence(TAS_JBUF_DONE);
    expect_silence(TAS_JBUF_DONE);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(80, stats.played_ms);
    TEST_ASSERT_EQUAL_UINT32(2, stats.lost);
    TEST_ASSERT_EQUAL_UINT32(0, stats.underruns);
}

static void test_drain_starts_a_stream_shorter_than_the_prefill(void) {
    push_range(0, 1);
    expect_silence(TAS_JBUF_PREFILL);

    tas_jbuf_drain(&jb, 1);
    expect_play(0);
    expect_play(1);
    expect_silence(TAS_JBUF_DONE);
}

static bool try_push(uint32_t seq) {
    const int16_t pcm[FRAME] = {0};
    return tas_jbuf_push(&jb, seq, pcm, FRAME);
}

static void test_late_duplicate_and_trailing_frames_are_dropped(void) {
    push(0);
    push_range(2, 4);
    expect_play(0);
    expect_silence(TAS_JBUF_LOST);

    TEST_ASSERT_FALSE(try_push(0)); // already played
    TEST_ASSERT_FALSE(try_push(1)); // already concealed
    TEST_ASSERT_FALSE(try_push(3)); // a duplicate
    tas_jbuf_drain(&jb, 5);
    TEST_ASSERT_FALSE(try_push(6)); // past last_seq

    expect_play(2);
    expect_play(3);
}

static void test_overflow_drops_the_oldest_frames_and_skips_past_them(void) {
    push_range(0, 3);
    expect_play(0);
    push_range(4, 20); // holds 1..20: 200 ms

    push(21);
    expect_play(2);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(1, stats.lost);
    TEST_ASSERT_EQUAL_UINT32(30, stats.played_ms);
}

static void test_an_overflow_skips_to_the_oldest_frame_held(void) {
    push_range(0, 3);
    expect_play(0);

    push(25); // 25 is 200 ms past the playhead: 1-3 drop, and nothing is held before 25
    expect_play(25);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(24, stats.lost);
    TEST_ASSERT_EQUAL_UINT32(260, stats.played_ms);
}

static void test_playback_resumes_at_the_oldest_frame_held(void) {
    push_range(0, 3);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);
    expect_silence(TAS_JBUF_UNDERRUN);

    push(6); // 4 and 5 never came: the pause already stood in for them
    expect_play(6);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(2, stats.lost);
    TEST_ASSERT_EQUAL_UINT32(70, stats.played_ms);
}

static void test_a_blackout_longer_than_the_buffer_resumes_without_added_latency(void) {
    push_range(0, 3);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);
    expect_silence(TAS_JBUF_UNDERRUN);

    push(40);
    push(41);
    expect_play(40);
    expect_play(41);
    TEST_ASSERT_EQUAL_UINT32(36, tas_jbuf_stats(&jb).lost);
}

static void test_without_prefill_playback_waits_for_the_first_frame(void) {
    tas_jbuf_config_t cfg = CONFIG;
    cfg.prefill_ms = 0;
    TEST_ASSERT_TRUE(tas_jbuf_start(&jb, &cfg));

    expect_silence(TAS_JBUF_PREFILL);
    push(0);
    expect_play(0);
    TEST_ASSERT_EQUAL_UINT32(0, tas_jbuf_stats(&jb).underruns);
}

static void test_a_prefill_beyond_the_capacity_is_capped(void) {
    tas_jbuf_config_t cfg = CONFIG;
    cfg.prefill_ms = 500;
    TEST_ASSERT_TRUE(tas_jbuf_start(&jb, &cfg));

    push_range(0, 19);
    expect_play(0);
}

static void push_short(uint32_t seq, size_t samples) {
    int16_t pcm[FRAME];
    for (size_t i = 0; i < samples; i++) pcm[i] = level(seq);
    TEST_ASSERT_TRUE(tas_jbuf_push(&jb, seq, pcm, samples));
}

static void test_a_short_final_frame_plays_padded_with_silence(void) {
    push_range(0, 3);
    push_short(4, 50);
    tas_jbuf_drain(&jb, 4);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);

    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, tas_jbuf_pull(&jb, out));
    TEST_ASSERT_EQUAL_INT16(level(4), out[10]);
    for (size_t i = 50; i < FRAME; i++) TEST_ASSERT_EQUAL_INT16(0, out[i]);
    expect_silence(TAS_JBUF_DONE);
    TEST_ASSERT_EQUAL_UINT32(43, tas_jbuf_stats(&jb).played_ms); // 40 ms + 50 samples
}

static void test_a_short_frame_is_held_until_the_drain_says_it_is_final(void) {
    push_range(0, 3);
    push_short(4, 100);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);

    expect_silence(TAS_JBUF_UNDERRUN);
    tas_jbuf_drain(&jb, 4);
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, tas_jbuf_pull(&jb, out));
    TEST_ASSERT_EQUAL_INT16(level(4), out[50]); // between the resume and end ramps
}

static void test_a_short_frame_that_is_not_the_final_one_is_dropped(void) {
    push_range(0, 3);
    push_short(4, 50);
    push(5);
    push_short(6, 50);
    tas_jbuf_drain(&jb, 7);
    for (uint32_t seq = 0; seq <= 3; seq++) expect_play(seq);

    expect_silence(TAS_JBUF_LOST);
    expect_play(5);
    expect_silence(TAS_JBUF_LOST);

    tas_jbuf_stats_t stats = tas_jbuf_stats(&jb);
    TEST_ASSERT_EQUAL_UINT32(2, stats.short_frames);
    TEST_ASSERT_EQUAL_UINT32(2, stats.lost);
}

static void test_an_empty_or_oversized_frame_is_dropped(void) {
    const int16_t pcm[FRAME + 1] = {0};
    TEST_ASSERT_FALSE(tas_jbuf_push(&jb, 0, pcm, 0));
    TEST_ASSERT_FALSE(tas_jbuf_push(&jb, 0, pcm, FRAME + 1));
    TEST_ASSERT_TRUE(tas_jbuf_push(&jb, 0, pcm, FRAME));
}

static void test_a_config_that_does_not_fit_is_refused(void) {
    tas_jbuf_config_t cfg = CONFIG;
    cfg.frame_ms = 20;
    TEST_ASSERT_TRUE(tas_jbuf_start(&jb, &cfg));

    cfg = CONFIG;
    cfg.rate = 48000; // 200 ms is 9600 samples
    TEST_ASSERT_FALSE(tas_jbuf_start(&jb, &cfg));
    cfg = CONFIG;
    cfg.frame_ms = 0;
    TEST_ASSERT_FALSE(tas_jbuf_start(&jb, &cfg));
    cfg = CONFIG;
    cfg.capacity_ms = 5; // less than a frame
    TEST_ASSERT_FALSE(tas_jbuf_start(&jb, &cfg));
    cfg = CONFIG;
    cfg.ramp_ms = 20; // longer than a frame
    TEST_ASSERT_FALSE(tas_jbuf_start(&jb, &cfg));
}

// A 50 Hz cosine at 30000: every frame boundary sits on a peak, so an edge without a ramp jumps
// by the full level, while the signal itself never moves more than ~600 per sample.
#define TONE_LEVEL 30000
#define PI 3.14159265358979
#define RAMP 32

static int16_t tone_at(uint64_t n) {
    return (int16_t)lround(TONE_LEVEL * cos(2 * PI * 50.0 * (double)n / 16000.0));
}

static void push_tone(uint32_t seq) {
    int16_t pcm[FRAME];
    for (size_t i = 0; i < FRAME; i++) pcm[i] = tone_at((uint64_t)seq * FRAME + i);
    TEST_ASSERT_TRUE(tas_jbuf_push(&jb, seq, pcm, FRAME));
}

static int16_t heard[64 * FRAME];
static size_t heard_n;

static tas_jbuf_out_t listen(void) {
    tas_jbuf_out_t kind = tas_jbuf_pull(&jb, heard + heard_n);
    heard_n += FRAME;
    return kind;
}

static void push_short_tone(uint32_t seq, size_t samples) {
    int16_t pcm[FRAME];
    for (size_t i = 0; i < samples; i++) pcm[i] = tone_at((uint64_t)seq * FRAME + i);
    TEST_ASSERT_TRUE(tas_jbuf_push(&jb, seq, pcm, samples));
}

static void test_every_edge_is_ramped(void) {
    heard_n = 0;
    for (uint32_t seq = 0; seq <= 3; seq++) push_tone(seq);
    push_tone(5);
    push_tone(6);

    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen()); // start
    listen();
    listen();
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen()); // 3, before a gap
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_LOST, listen());
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen()); // 5
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen()); // 6, before an underrun
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_UNDERRUN, listen());

    for (uint32_t seq = 8; seq <= 29; seq++) push_tone(seq); // 29 overflows: 9 drops
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen()); // 8, resumed past the missing 7
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen()); // 10: a jump mid-signal, from +peak to -peak
    push_short_tone(60, 100); // overflows to a frame that can't play yet
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_UNDERRUN, listen()); // right after audio that didn't ramp down

    tas_jbuf_drain(&jb, 60);
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_PLAY, listen());
    TEST_ASSERT_EQUAL_INT(TAS_JBUF_DONE, listen());

    int worst = abs(heard[0]); // from the silence before the stream
    for (size_t i = 1; i < heard_n; i++) {
        int step = abs(heard[i] - heard[i - 1]);
        if (step > worst) worst = step;
    }
    TEST_ASSERT_LESS_OR_EQUAL_INT(2 * TONE_LEVEL / RAMP + 600, worst);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_playback_waits_for_the_prefill);
    RUN_TEST(test_a_gap_with_later_frames_buffered_is_lost_and_skipped);
    RUN_TEST(test_an_empty_buffer_pauses_without_skipping);
    RUN_TEST(test_after_drain_missing_frames_are_concealed_until_last_seq);
    RUN_TEST(test_drain_starts_a_stream_shorter_than_the_prefill);
    RUN_TEST(test_late_duplicate_and_trailing_frames_are_dropped);
    RUN_TEST(test_overflow_drops_the_oldest_frames_and_skips_past_them);
    RUN_TEST(test_an_overflow_skips_to_the_oldest_frame_held);
    RUN_TEST(test_playback_resumes_at_the_oldest_frame_held);
    RUN_TEST(test_a_blackout_longer_than_the_buffer_resumes_without_added_latency);
    RUN_TEST(test_without_prefill_playback_waits_for_the_first_frame);
    RUN_TEST(test_a_prefill_beyond_the_capacity_is_capped);
    RUN_TEST(test_a_short_final_frame_plays_padded_with_silence);
    RUN_TEST(test_a_short_frame_is_held_until_the_drain_says_it_is_final);
    RUN_TEST(test_a_short_frame_that_is_not_the_final_one_is_dropped);
    RUN_TEST(test_every_edge_is_ramped);
    RUN_TEST(test_a_config_that_does_not_fit_is_refused);
    RUN_TEST(test_an_empty_or_oversized_frame_is_dropped);
    return UNITY_END();
}
