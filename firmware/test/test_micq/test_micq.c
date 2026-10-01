#include <unity.h>

#include "tas_micq.h"

#define FRAME 160 // 10 ms at 16 kHz

static tas_micq_t q;

static const tas_micq_config_t CONFIG = {.rate = 16000, .frame_ms = 10, .capacity_ms = 200};

// Captures a frame whose samples all hold `level`, at device time `ts`.
static void capture(int16_t level, uint64_t ts) {
    int16_t pcm[FRAME];
    for (size_t i = 0; i < FRAME; i++) pcm[i] = level;
    tas_micq_push(&q, pcm, ts);
}

static void expect_frame(uint32_t seq, int16_t level, uint64_t ts) {
    tas_micq_frame_t frame;
    TEST_ASSERT_TRUE_MESSAGE(tas_micq_pop(&q, &frame), "no frame queued");
    TEST_ASSERT_EQUAL_UINT32(seq, frame.seq);
    TEST_ASSERT_EQUAL_UINT64(ts, frame.ts);
    TEST_ASSERT_EQUAL_size_t(FRAME, frame.samples);
    TEST_ASSERT_EQUAL_INT16(level, frame.pcm[0]);
    TEST_ASSERT_EQUAL_INT16(level, frame.pcm[FRAME - 1]);
}

static void expect_empty(void) {
    tas_micq_frame_t frame;
    TEST_ASSERT_FALSE(tas_micq_pop(&q, &frame));
}

void setUp(void) {
    TEST_ASSERT_TRUE(tas_micq_start(&q, &CONFIG));
}

void tearDown(void) {}

static void test_frames_go_out_in_order_numbered_from_zero(void) {
    capture(100, 1000);
    capture(200, 11000);

    expect_frame(0, 100, 1000);
    expect_frame(1, 200, 11000);
    expect_empty();
}

static void test_last_seq_is_the_newest_frame_captured_and_none_before_the_first(void) {
    uint32_t last;
    TEST_ASSERT_FALSE(tas_micq_last_seq(&q, &last));

    capture(100, 1000);
    capture(200, 11000);
    tas_micq_frame_t frame;
    tas_micq_pop(&q, &frame);

    TEST_ASSERT_TRUE(tas_micq_last_seq(&q, &last));
    TEST_ASSERT_EQUAL_UINT32(1, last);
}

static void test_a_full_queue_drops_its_oldest_frames_and_counts_them(void) {
    for (int16_t i = 0; i < 23; i++) capture(i, 1000 + 10000 * (uint64_t)i);

    TEST_ASSERT_EQUAL_UINT32(3, q.dropped);
    for (uint32_t seq = 3; seq < 23; seq++) expect_frame(seq, (int16_t)seq, 1000 + 10000 * seq);
    expect_empty();
    uint32_t last;
    TEST_ASSERT_TRUE(tas_micq_last_seq(&q, &last));
    TEST_ASSERT_EQUAL_UINT32(22, last);
}

static void test_the_queue_holds_200_ms_of_20_ms_frames(void) {
    tas_micq_config_t config = CONFIG;
    config.frame_ms = 20;
    TEST_ASSERT_TRUE(tas_micq_start(&q, &config));
    const int16_t pcm[2 * FRAME] = {0};

    for (int i = 0; i < 11; i++) tas_micq_push(&q, pcm, 0);

    TEST_ASSERT_EQUAL_UINT32(1, q.dropped);
    tas_micq_frame_t frame;
    TEST_ASSERT_TRUE(tas_micq_pop(&q, &frame));
    TEST_ASSERT_EQUAL_UINT32(1, frame.seq);
    TEST_ASSERT_EQUAL_size_t(2 * FRAME, frame.samples);
}

static void test_a_new_stream_starts_over(void) {
    capture(100, 1000);
    for (int i = 0; i < 25; i++) capture(0, 0);

    TEST_ASSERT_TRUE(tas_micq_start(&q, &CONFIG));

    uint32_t last;
    TEST_ASSERT_FALSE(tas_micq_last_seq(&q, &last));
    TEST_ASSERT_EQUAL_UINT32(0, q.dropped);
    expect_empty();
    capture(300, 5000);
    expect_frame(0, 300, 5000);
}

static void test_a_config_past_the_storage_is_refused(void) {
    tas_micq_config_t config = CONFIG;
    config.capacity_ms = 400;
    TEST_ASSERT_FALSE(tas_micq_start(&q, &config));
    config = CONFIG;
    config.frame_ms = 0;
    TEST_ASSERT_FALSE(tas_micq_start(&q, &config));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_frames_go_out_in_order_numbered_from_zero);
    RUN_TEST(test_last_seq_is_the_newest_frame_captured_and_none_before_the_first);
    RUN_TEST(test_a_full_queue_drops_its_oldest_frames_and_counts_them);
    RUN_TEST(test_the_queue_holds_200_ms_of_20_ms_frames);
    RUN_TEST(test_a_new_stream_starts_over);
    RUN_TEST(test_a_config_past_the_storage_is_refused);
    return UNITY_END();
}
