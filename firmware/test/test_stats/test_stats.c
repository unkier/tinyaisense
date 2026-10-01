#include <unity.h>

#include "tas_stats.h"

void setUp(void) {}
void tearDown(void) {}

// Nearest rank: the smallest sample with at least pct% of the samples at or below it.
static void test_percentiles_of_a_hundred_samples_are_their_ranks(void) {
    uint32_t v[100];
    for (uint32_t i = 0; i < 100; i++) v[i] = (i * 37 + 11) % 100 + 1; // 1..100, shuffled

    TEST_ASSERT_EQUAL_UINT32(50, tas_percentile(v, 100, 50));
    TEST_ASSERT_EQUAL_UINT32(95, tas_percentile(v, 100, 95));
    TEST_ASSERT_EQUAL_UINT32(99, tas_percentile(v, 100, 99));
    TEST_ASSERT_EQUAL_UINT32(100, tas_percentile(v, 100, 100));
}

static void test_a_percentile_between_ranks_rounds_up(void) {
    uint32_t v[] = {30, 10, 20};

    TEST_ASSERT_EQUAL_UINT32(10, tas_percentile(v, 3, 1));
    TEST_ASSERT_EQUAL_UINT32(20, tas_percentile(v, 3, 50));
    TEST_ASSERT_EQUAL_UINT32(30, tas_percentile(v, 3, 99));
}

static void test_no_samples_give_zero(void) {
    TEST_ASSERT_EQUAL_UINT32(0, tas_percentile(NULL, 0, 99));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_percentiles_of_a_hundred_samples_are_their_ranks);
    RUN_TEST(test_a_percentile_between_ranks_rounds_up);
    RUN_TEST(test_no_samples_give_zero);
    return UNITY_END();
}
