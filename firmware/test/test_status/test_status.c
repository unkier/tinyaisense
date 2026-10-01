#include <string.h>
#include <unity.h>

#include "tas_status.h"

static tas_status_layers_t layers;

static tas_status_t shown(void) {
    return tas_status_shown(&layers);
}

void setUp(void) {
    layers = (tas_status_layers_t){.hub = TAS_STATUS_NO_AGENT};
}

void tearDown(void) {}

static void test_with_no_device_state_the_hub_layer_shows(void) {
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_NO_AGENT, shown());
    layers.hub = TAS_STATUS_AGENT;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_AGENT, shown());
}

static void test_the_device_states_go_by_the_priority_table(void) {
    layers.speaking = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_SPEAKING, shown());
    layers.no_config = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_NO_CONFIG, shown());
    layers.pending = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_PENDING, shown());
    layers.connecting = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_CONNECTING, shown());
    layers.offline = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_OFFLINE, shown());
    layers.claimed = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_CLAIM, shown());
    layers.listening = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_LISTENING, shown());
    layers.identify = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_IDENTIFY, shown());
}

static void test_speaking_outranks_thinking(void) {
    layers.hub = TAS_STATUS_THINKING;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_THINKING, shown());
    layers.speaking = true;
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_SPEAKING, shown());
}

static void test_the_hub_layer_takes_only_hub_states(void) {
    tas_status_t layer;
    TEST_ASSERT_TRUE(tas_status_hub_layer("thinking", 8, &layer));
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_THINKING, layer);
    TEST_ASSERT_TRUE(tas_status_hub_layer("no_agent", 8, &layer));
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_NO_AGENT, layer);
    TEST_ASSERT_TRUE(tas_status_hub_layer("agent", 5, &layer));
    TEST_ASSERT_EQUAL_INT(TAS_STATUS_AGENT, layer);
    TEST_ASSERT_FALSE(tas_status_hub_layer("listening", 9, &layer));
    TEST_ASSERT_FALSE(tas_status_hub_layer("agen", 4, &layer));
}

static void test_states_have_their_documented_names(void) {
    TEST_ASSERT_EQUAL_STRING("no_config", tas_status_name(TAS_STATUS_NO_CONFIG));
    TEST_ASSERT_EQUAL_STRING("no_agent", tas_status_name(TAS_STATUS_NO_AGENT));
}

static void test_solid_is_always_full(void) {
    TEST_ASSERT_EQUAL_UINT8(255, tas_pattern_level(TAS_PATTERN_SOLID, 1000, 0));
    TEST_ASSERT_EQUAL_UINT8(255, tas_pattern_level(TAS_PATTERN_SOLID, 1000, 777));
}

static void test_a_4_hz_blink_is_on_for_125_ms_then_off_for_125_ms(void) {
    TEST_ASSERT_EQUAL_UINT8(255, tas_pattern_level(TAS_PATTERN_BLINK, 250, 0));
    TEST_ASSERT_EQUAL_UINT8(255, tas_pattern_level(TAS_PATTERN_BLINK, 250, 124));
    TEST_ASSERT_EQUAL_UINT8(0, tas_pattern_level(TAS_PATTERN_BLINK, 250, 125));
    TEST_ASSERT_EQUAL_UINT8(0, tas_pattern_level(TAS_PATTERN_BLINK, 250, 249));
    TEST_ASSERT_EQUAL_UINT8(255, tas_pattern_level(TAS_PATTERN_BLINK, 250, 250));
}

static void test_a_2_s_pulse_is_dark_at_the_start_and_full_at_one_second(void) {
    TEST_ASSERT_EQUAL_UINT8(0, tas_pattern_level(TAS_PATTERN_PULSE, 2000, 0));
    TEST_ASSERT_EQUAL_UINT8(255, tas_pattern_level(TAS_PATTERN_PULSE, 2000, 1000));
    TEST_ASSERT_EQUAL_UINT8(0, tas_pattern_level(TAS_PATTERN_PULSE, 2000, 4000));
    uint8_t rising = tas_pattern_level(TAS_PATTERN_PULSE, 2000, 500);
    TEST_ASSERT_TRUE(rising > 0 && rising < 255);
    TEST_ASSERT_EQUAL_UINT8(rising, tas_pattern_level(TAS_PATTERN_PULSE, 2000, 1500));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_with_no_device_state_the_hub_layer_shows);
    RUN_TEST(test_the_device_states_go_by_the_priority_table);
    RUN_TEST(test_speaking_outranks_thinking);
    RUN_TEST(test_the_hub_layer_takes_only_hub_states);
    RUN_TEST(test_states_have_their_documented_names);
    RUN_TEST(test_solid_is_always_full);
    RUN_TEST(test_a_4_hz_blink_is_on_for_125_ms_then_off_for_125_ms);
    RUN_TEST(test_a_2_s_pulse_is_dark_at_the_start_and_full_at_one_second);
    return UNITY_END();
}
