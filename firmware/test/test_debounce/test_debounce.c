// A button's edges as the reflex task sees them: the interrupt's times, then the pin re-read at
// each deadline.

#include <unity.h>

#include "tas_debounce.h"

static tas_debounce_t button;
static tas_edge_t edge;

void setUp(void) {
    tas_debounce_init(&button, false);
}

void tearDown(void) {}

static void test_a_press_is_reported_once_confirmed_timed_by_its_first_edge(void) {
    tas_debounce_edge(&button, 5000);
    TEST_ASSERT_EQUAL_UINT64(6000, tas_debounce_deadline(&button));
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 6000, true, &edge));
    TEST_ASSERT_TRUE(edge.down);
    TEST_ASSERT_EQUAL_UINT64(5000, edge.ts);
}

static void test_a_glitch_the_re_read_doesnt_confirm_is_ignored(void) {
    tas_debounce_edge(&button, 5000);
    TEST_ASSERT_FALSE(tas_debounce_check(&button, 6000, false, &edge));
    TEST_ASSERT_EQUAL_UINT64(TAS_DEBOUNCE_NONE, tas_debounce_deadline(&button));
    // The next press is still reported.
    tas_debounce_edge(&button, 9000);
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 10000, true, &edge));
    TEST_ASSERT_EQUAL_UINT64(9000, edge.ts);
}

static void test_bounces_add_no_edges(void) {
    tas_debounce_edge(&button, 5000);
    tas_debounce_edge(&button, 5200); // bounces before the re-read
    tas_debounce_edge(&button, 5400);
    TEST_ASSERT_EQUAL_UINT64(6000, tas_debounce_deadline(&button));
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 6000, true, &edge));
    TEST_ASSERT_EQUAL_UINT64(5000, edge.ts);
    tas_debounce_edge(&button, 6500); // and after it
    tas_debounce_edge(&button, 7000);
    TEST_ASSERT_EQUAL_UINT64(26000, tas_debounce_deadline(&button));
    TEST_ASSERT_FALSE(tas_debounce_check(&button, 26000, true, &edge));
    TEST_ASSERT_EQUAL_UINT64(TAS_DEBOUNCE_NONE, tas_debounce_deadline(&button));
    // The release.
    tas_debounce_edge(&button, 90000);
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 91000, false, &edge));
    TEST_ASSERT_FALSE(edge.down);
    TEST_ASSERT_EQUAL_UINT64(90000, edge.ts);
}

static void test_a_release_inside_the_lockout_is_reported_once_it_ends(void) {
    tas_debounce_edge(&button, 5000);
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 6000, true, &edge));
    tas_debounce_edge(&button, 6500); // a bounce
    tas_debounce_edge(&button, 12000); // a quick release, and its bounce
    tas_debounce_edge(&button, 12300);
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 26000, false, &edge));
    TEST_ASSERT_FALSE(edge.down);
    TEST_ASSERT_EQUAL_UINT64(12300, edge.ts); // the last edge it saw
    // Bounces past it are locked out too.
    TEST_ASSERT_EQUAL_UINT64(46000, tas_debounce_deadline(&button));
    TEST_ASSERT_FALSE(tas_debounce_check(&button, 46000, false, &edge));
}

static void test_a_change_with_no_edge_seen_in_the_lockout_is_timed_by_its_end(void) {
    tas_debounce_edge(&button, 5000);
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 6000, true, &edge));
    TEST_ASSERT_TRUE(tas_debounce_check(&button, 26000, false, &edge));
    TEST_ASSERT_EQUAL_UINT64(26000, edge.ts);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_a_press_is_reported_once_confirmed_timed_by_its_first_edge);
    RUN_TEST(test_a_glitch_the_re_read_doesnt_confirm_is_ignored);
    RUN_TEST(test_bounces_add_no_edges);
    RUN_TEST(test_a_release_inside_the_lockout_is_reported_once_it_ends);
    RUN_TEST(test_a_change_with_no_edge_seen_in_the_lockout_is_timed_by_its_end);
    return UNITY_END();
}
