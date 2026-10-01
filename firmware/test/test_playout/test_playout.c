// The playout tracker, driven the way the I2S driver drives the speaker's ring: on_sent as each
// descriptor finishes, writes into the oldest free one.

#include <unity.h>

#include "tas_playout.h"

#define DESCS 8
#define PER_FRAME 4 // descriptors: a 10 ms frame in 2.5 ms descriptors

static tas_playout_t p;
static uint32_t sents; // descriptors the DMA has finished

// The DMA finishes `n` more descriptors.
static void play(uint32_t n) {
    while (n--) tas_playout_sent(&p, ++sents);
}

// The speaker writes a frame into free descriptors; once played, the stream has played `ms`.
static void write_frame(uint32_t ms) {
    for (int i = 0; i < PER_FRAME; i++) tas_playout_wrote(&p);
    tas_jbuf_stats_t stats = {.played_ms = ms};
    tas_playout_frame(&p, &stats);
}

static uint32_t heard_ms(void) {
    return tas_playout_heard(&p).played_ms;
}

void setUp(void) {
    sents = 0;
    tas_playout_init(&p, DESCS); // the channel starts on a ring of silence, none of it free
    tas_playout_stream(&p);
}

void tearDown(void) {}

static void test_a_frame_is_heard_once_its_last_descriptor_has_played_a_lap_later(void) {
    play(4); // frees the first four

    write_frame(10);

    play(7);
    TEST_ASSERT_EQUAL_UINT32(0, heard_ms());
    play(1);
    TEST_ASSERT_EQUAL_UINT32(10, heard_ms());
}

static void test_on_a_ring_left_idle_the_first_write_plays_next(void) {
    play(20); // nobody writes: every descriptor but the one playing is free

    write_frame(10);

    play(4);
    TEST_ASSERT_EQUAL_UINT32(0, heard_ms());
    play(1);
    TEST_ASSERT_EQUAL_UINT32(10, heard_ms());
}

static void test_only_frames_that_have_played_count_as_heard(void) {
    play(20);
    write_frame(10);
    play(4); // the writer kept up: the next frame is written as descriptors free up
    write_frame(20);

    play(1);

    TEST_ASSERT_EQUAL_UINT32(10, heard_ms());
}

static void test_the_ring_is_idle_once_the_last_write_has_played(void) {
    TEST_ASSERT_TRUE(tas_playout_idle(&p));
    play(20);
    write_frame(10);

    play(4);
    TEST_ASSERT_FALSE(tas_playout_idle(&p));
    play(1);
    TEST_ASSERT_TRUE(tas_playout_idle(&p));
}

static void test_a_new_stream_has_heard_nothing_of_the_last_ones_frames(void) {
    play(20);
    write_frame(10);
    play(5);
    write_frame(20); // cut before it plays

    tas_playout_stream(&p);
    play(8);

    TEST_ASSERT_EQUAL_UINT32(0, heard_ms());
}

static void test_a_new_stream_is_idle_until_it_writes(void) {
    play(20);
    write_frame(10); // cut before it plays

    tas_playout_stream(&p);

    TEST_ASSERT_TRUE(tas_playout_idle(&p));
}

static void test_the_sent_count_may_wrap(void) {
    sents = UINT32_MAX - 1;
    play(20);
    write_frame(10);

    play(4);
    TEST_ASSERT_EQUAL_UINT32(0, heard_ms());
    TEST_ASSERT_FALSE(tas_playout_idle(&p));
    play(1);
    TEST_ASSERT_EQUAL_UINT32(10, heard_ms());
    TEST_ASSERT_TRUE(tas_playout_idle(&p));
}

static void test_after_a_restart_the_preloaded_ring_plays_before_any_write(void) {
    play(20);
    tas_playout_restart(&p);

    play(4);
    write_frame(10);

    play(7);
    TEST_ASSERT_EQUAL_UINT32(0, heard_ms());
    play(1);
    TEST_ASSERT_EQUAL_UINT32(10, heard_ms());
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_a_frame_is_heard_once_its_last_descriptor_has_played_a_lap_later);
    RUN_TEST(test_on_a_ring_left_idle_the_first_write_plays_next);
    RUN_TEST(test_only_frames_that_have_played_count_as_heard);
    RUN_TEST(test_the_ring_is_idle_once_the_last_write_has_played);
    RUN_TEST(test_a_new_stream_has_heard_nothing_of_the_last_ones_frames);
    RUN_TEST(test_a_new_stream_is_idle_until_it_writes);
    RUN_TEST(test_the_sent_count_may_wrap);
    RUN_TEST(test_after_a_restart_the_preloaded_ring_plays_before_any_write);
    return UNITY_END();
}
