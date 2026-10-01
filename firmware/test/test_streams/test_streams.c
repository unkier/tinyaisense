#include <unity.h>

#include "tas_streams.h"

static tas_closed_t closed;

void setUp(void) {
    tas_closed_clear(&closed);
}

void tearDown(void) {}

static void test_the_last_16_closed_ids_are_remembered(void) {
    for (uint8_t id = 1; id <= 20; id++) tas_closed_add(&closed, id);

    for (uint8_t id = 1; id <= 4; id++) TEST_ASSERT_FALSE(tas_closed_has(&closed, id));
    for (uint8_t id = 5; id <= 20; id++) TEST_ASSERT_TRUE(tas_closed_has(&closed, id));
    TEST_ASSERT_FALSE(tas_closed_has(&closed, 21));
}

static void test_a_new_connection_remembers_none(void) {
    tas_closed_add(&closed, 7);

    tas_closed_clear(&closed);

    TEST_ASSERT_FALSE(tas_closed_has(&closed, 7));
    TEST_ASSERT_FALSE(tas_closed_has(&closed, 0));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_the_last_16_closed_ids_are_remembered);
    RUN_TEST(test_a_new_connection_remembers_none);
    return UNITY_END();
}
