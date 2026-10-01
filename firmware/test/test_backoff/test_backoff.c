#include <stdint.h>
#include <unity.h>

#include "tas_backoff.h"

static tas_backoff_t backoff;

void setUp(void) {
    backoff = (tas_backoff_t){0};
}

void tearDown(void) {}

static void test_the_wait_doubles_up_to_the_five_second_cap(void) {
    static const uint32_t steps[] = {500, 1000, 2000, 5000, 5000, 5000};
    for (int i = 0; i < 6; i++) {
        TEST_ASSERT_EQUAL_UINT32(steps[i], tas_backoff_failed(&backoff, 0).delay_ms);
    }
}

static void test_the_jitter_adds_up_to_a_fifth_of_the_step(void) {
    TEST_ASSERT_EQUAL_UINT32(599, tas_backoff_failed(&backoff, UINT32_MAX).delay_ms);
    TEST_ASSERT_EQUAL_UINT32(1100, tas_backoff_failed(&backoff, UINT32_MAX / 2 + 1).delay_ms);
}

static void test_mdns_is_resolved_again_after_every_third_failure_in_a_row(void) {
    for (int failure = 1; failure <= 9; failure++) {
        TEST_ASSERT_EQUAL(failure % 3 == 0, tas_backoff_failed(&backoff, 0).resolve);
    }
}

static void test_welcome_starts_the_count_over(void) {
    tas_backoff_failed(&backoff, 0);
    tas_backoff_failed(&backoff, 0);
    tas_backoff_reset(&backoff);

    tas_retry_t retry = tas_backoff_failed(&backoff, 0);
    TEST_ASSERT_EQUAL_UINT32(500, retry.delay_ms);
    TEST_ASSERT_FALSE(retry.resolve);
    TEST_ASSERT_FALSE(tas_backoff_failed(&backoff, 0).resolve);
    TEST_ASSERT_TRUE(tas_backoff_failed(&backoff, 0).resolve);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_the_wait_doubles_up_to_the_five_second_cap);
    RUN_TEST(test_the_jitter_adds_up_to_a_fifth_of_the_step);
    RUN_TEST(test_mdns_is_resolved_again_after_every_third_failure_in_a_row);
    RUN_TEST(test_welcome_starts_the_count_over);
    return UNITY_END();
}
