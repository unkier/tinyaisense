// The led Kind on one capability, driven by commands as the hub sends them.

#include <string.h>
#include <unity.h>

#include "tas_led.h"

#define CAP 64 // the Atom Echo's brightness cap

static tas_led_t led;
static tas_arena_t arena;
static tas_msg_t reply;

void setUp(void) {
    tas_led_init(&led, 2);
}

void tearDown(void) {
    tas_arena_free(&arena);
}

// Sends `name` with `args` (a JSON object) at `now_ms`; the reply lands in `reply`.
static void command_at(const char *name, const char *args, uint64_t now_ms) {
    tas_msg_t cmd = {.type = TAS_MSG_CMD};
    cmd.cmd.id = 1;
    cmd.cmd.has_cap = true;
    cmd.cmd.cap = tas_str("led");
    cmd.cmd.name = tas_str(name);
    tas_json_t *parsed;
    TEST_ASSERT_EQUAL_INT(TAS_JSON_OK,
                          tas_json_parse((const uint8_t *)args, strlen(args), &arena, &parsed));
    cmd.cmd.args = parsed;
    reply = (tas_msg_t){.type = TAS_MSG_REPLY};
    reply.reply.id = 1;
    reply.reply.ok = true;
    reply.reply.result = tas_json_object(&arena);
    tas_led_command(&led, &cmd, now_ms, &reply);
}

static void command(const char *name, const char *args) {
    command_at(name, args, 0);
}

static void assert_ok(void) {
    TEST_ASSERT_TRUE_MESSAGE(reply.reply.ok, reply.reply.message.ptr);
}

static void assert_rgb(uint8_t r, uint8_t g, uint8_t b, size_t index, uint64_t now_ms) {
    tas_rgb_t rgb = tas_led_rgb(&led, index, CAP, now_ms);
    uint8_t want[] = {r, g, b}, got[] = {rgb.r, rgb.g, rgb.b};
    TEST_ASSERT_EQUAL_UINT8_ARRAY(want, got, 3);
}

static void test_a_set_with_only_a_color_lights_every_led_solid_at_the_brightness_cap(void) {
    command("set", "{\"color\":\"#ff8000\"}");
    assert_ok();
    TEST_ASSERT_TRUE(led.claimed);
    // 100 is the cap, 64 of 255.
    assert_rgb(64, 32, 0, 0, 0);
    assert_rgb(64, 32, 0, 1, 0);
    assert_rgb(64, 32, 0, 1, 12345);
}

static void test_brightness_pattern_and_period_shape_the_look_from_when_it_was_set(void) {
    command_at("set", "{\"color\":\"#FFFFFF\",\"brightness\":50,\"pattern\":\"blink\","
                      "\"period_ms\":400}",
               1000);
    assert_ok();
    // Half of the cap; on for the first 200 ms of each 400 ms from the set on.
    assert_rgb(32, 32, 32, 0, 1000);
    assert_rgb(32, 32, 32, 1, 1199);
    assert_rgb(0, 0, 0, 0, 1200);
    assert_rgb(32, 32, 32, 0, 1400);
}

static void test_a_pulse_with_the_default_period_peaks_half_a_second_in(void) {
    command_at("set", "{\"color\":\"#00ff00\",\"pattern\":\"pulse\"}", 300);
    assert_ok();
    assert_rgb(0, 0, 0, 0, 300);
    assert_rgb(0, 64, 0, 0, 800);
    assert_rgb(0, 0, 0, 0, 1300);
}

static void assert_failed(const char *code) {
    TEST_ASSERT_FALSE(reply.reply.ok);
    TEST_ASSERT_EQUAL_STRING(code, reply.reply.code.ptr);
    TEST_ASSERT_TRUE(reply.reply.message.len > 0);
}

static void test_a_claim_starts_all_off_so_a_set_with_an_index_lights_only_that_led(void) {
    command("set", "{\"color\":\"#ff0000\",\"index\":1}");
    assert_ok();
    assert_rgb(0, 0, 0, 0, 0);
    assert_rgb(64, 0, 0, 1, 0);
    // The claim holds: a set on the other LED leaves this one as it was.
    command("set", "{\"index\":0,\"color\":\"#0000ff\"}");
    assert_ok();
    assert_rgb(0, 0, 64, 0, 0);
    assert_rgb(64, 0, 0, 1, 0);
}

static void test_an_index_past_the_last_led_is_invalid_and_claims_nothing(void) {
    command("set", "{\"color\":\"#ff0000\",\"index\":2}");
    assert_failed("invalid_args");
    TEST_ASSERT_FALSE(led.claimed);
}

static void test_args_that_break_the_contract_are_invalid_and_change_nothing(void) {
    command("set", "{\"color\":\"#00ff00\"}");
    static const char *const bad[] = {
        "{}",
        "{\"color\":\"00ff00\"}",
        "{\"color\":\"#00ff0\"}",
        "{\"color\":\"#00ff00 \"}",
        "{\"color\":\"#00fg00\"}",
        "{\"color\":255}",
        "{\"color\":\"#ff0000\",\"brightness\":101}",
        "{\"color\":\"#ff0000\",\"brightness\":-1}",
        "{\"color\":\"#ff0000\",\"brightness\":50.0}",
        "{\"color\":\"#ff0000\",\"brightness\":true}",
        "{\"color\":\"#ff0000\",\"pattern\":\"strobe\"}",
        "{\"color\":\"#ff0000\",\"pattern\":1}",
        "{\"color\":\"#ff0000\",\"period_ms\":199}",
        "{\"color\":\"#ff0000\",\"period_ms\":5001}",
        "{\"color\":\"#ff0000\",\"index\":-1}",
        "{\"color\":\"#ff0000\",\"index\":\"0\"}",
    };
    for (size_t i = 0; i < sizeof bad / sizeof *bad; i++) {
        command("set", bad[i]);
        TEST_ASSERT_FALSE_MESSAGE(reply.reply.ok, bad[i]);
        TEST_ASSERT_EQUAL_STRING_MESSAGE("invalid_args", reply.reply.code.ptr, bad[i]);
    }
    assert_rgb(0, 64, 0, 0, 0);
    assert_rgb(0, 64, 0, 1, 0);
}

static void test_unknown_args_are_ignored(void) {
    command("set", "{\"color\":\"#00ff00\",\"fade_ms\":300}");
    assert_ok();
    assert_rgb(0, 64, 0, 0, 0);
}

static void test_off_claims_the_leds_dark(void) {
    command("off", "{}");
    assert_ok();
    TEST_ASSERT_TRUE(led.claimed);
    command("set", "{\"color\":\"#ff0000\",\"index\":0}");
    command("off", "{}");
    assert_ok();
    TEST_ASSERT_TRUE(led.claimed);
    assert_rgb(0, 0, 0, 0, 0);
    // A set after it starts from dark, not from what was set before.
    command("set", "{\"color\":\"#0000ff\",\"index\":1}");
    assert_rgb(0, 0, 0, 0, 0);
    assert_rgb(0, 0, 64, 1, 0);
}

static void test_release_ends_the_claim_and_the_next_set_starts_a_fresh_one(void) {
    command("set", "{\"color\":\"#ff0000\"}");
    command("release", "{}");
    assert_ok();
    TEST_ASSERT_FALSE(led.claimed);
    command("set", "{\"color\":\"#0000ff\",\"index\":1}");
    assert_rgb(0, 0, 0, 0, 0);
    assert_rgb(0, 0, 64, 1, 0);
    // With nothing claimed, release does nothing and still succeeds.
    command("release", "{}");
    command("release", "{}");
    assert_ok();
}

static void test_the_end_of_the_connection_ends_the_claim(void) {
    command("set", "{\"color\":\"#ff0000\"}");
    tas_led_release(&led);
    TEST_ASSERT_FALSE(led.claimed);
    assert_rgb(0, 0, 0, 0, 0);
}

static void test_a_command_the_kind_lacks_is_unsupported(void) {
    command("fade", "{\"color\":\"#ff0000\"}");
    assert_failed("unsupported");
    TEST_ASSERT_FALSE(led.claimed);
}

static void test_brightness_rounds_to_the_nearest_level_as_the_simulator_does(void) {
    // 1% of 64 is 0.64: level 1, not off. 20% is 12.8: level 13.
    command("set", "{\"color\":\"#ffffff\",\"brightness\":1}");
    assert_rgb(1, 1, 1, 0, 0);
    command("set", "{\"color\":\"#ffffff\",\"brightness\":20}");
    assert_rgb(13, 13, 13, 0, 0);
}

static void test_a_look_read_just_before_its_set_shows_its_first_instant(void) {
    // The led task may take the time just before a command lands.
    command_at("set", "{\"color\":\"#ffffff\",\"pattern\":\"blink\"}", 1000);
    assert_rgb(64, 64, 64, 0, 999);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_a_set_with_only_a_color_lights_every_led_solid_at_the_brightness_cap);
    RUN_TEST(test_brightness_pattern_and_period_shape_the_look_from_when_it_was_set);
    RUN_TEST(test_a_pulse_with_the_default_period_peaks_half_a_second_in);
    RUN_TEST(test_a_claim_starts_all_off_so_a_set_with_an_index_lights_only_that_led);
    RUN_TEST(test_an_index_past_the_last_led_is_invalid_and_claims_nothing);
    RUN_TEST(test_args_that_break_the_contract_are_invalid_and_change_nothing);
    RUN_TEST(test_unknown_args_are_ignored);
    RUN_TEST(test_off_claims_the_leds_dark);
    RUN_TEST(test_release_ends_the_claim_and_the_next_set_starts_a_fresh_one);
    RUN_TEST(test_the_end_of_the_connection_ends_the_claim);
    RUN_TEST(test_a_command_the_kind_lacks_is_unsupported);
    RUN_TEST(test_brightness_rounds_to_the_nearest_level_as_the_simulator_does);
    RUN_TEST(test_a_look_read_just_before_its_set_shows_its_first_instant);
    return UNITY_END();
}
