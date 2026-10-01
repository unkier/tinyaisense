#include "tas_led.h"

void tas_led_init(tas_led_t *led, uint16_t count) {
    *led = (tas_led_t){.count = count < TAS_LED_MAX ? count : TAS_LED_MAX};
}

static int hex(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// "#rrggbb", hex digits in either case.
static bool parse_color(const tas_json_t *value, tas_rgb_t *rgb) {
    if (!value || value->type != TAS_JSON_STRING || value->text.len != 7 ||
        value->text.ptr[0] != '#') {
        return false;
    }
    uint8_t bytes[3];
    for (size_t i = 0; i < 3; i++) {
        int hi = hex(value->text.ptr[1 + 2 * i]), lo = hex(value->text.ptr[2 + 2 * i]);
        if (hi < 0 || lo < 0) return false;
        bytes[i] = (uint8_t)(hi * 16 + lo);
    }
    *rgb = (tas_rgb_t){bytes[0], bytes[1], bytes[2]};
    return true;
}

static const tas_look_t OFF = {{0, 0, 0}, 0, TAS_PATTERN_SOLID, 0};

static void show(tas_led_t *led, size_t index, const tas_look_t *look, uint64_t now_ms) {
    led->looks[index] = *look;
    led->since_ms[index] = now_ms;
}

// A claim starts with every LED off.
static void claim(tas_led_t *led, uint64_t now_ms) {
    if (led->claimed) return;
    led->claimed = true;
    for (size_t i = 0; i < led->count; i++) show(led, i, &OFF, now_ms);
}

static const char *const PATTERNS[] = {
    [TAS_PATTERN_SOLID] = "solid", [TAS_PATTERN_BLINK] = "blink", [TAS_PATTERN_PULSE] = "pulse"};

static void set(tas_led_t *led, const tas_json_t *args, uint64_t now_ms, tas_msg_t *reply) {
    tas_look_t look;
    if (!parse_color(tas_json_get(args, "color"), &look.color)) {
        return tas_reply_fail(reply, "invalid_args", "color must be \"#rrggbb\"");
    }
    int64_t brightness = 100, period_ms = 1000;
    size_t pattern = TAS_PATTERN_SOLID;
    if (tas_arg_int(args, "brightness", 0, 100, &brightness) == TAS_ARG_BAD) {
        return tas_reply_fail(reply, "invalid_args",
                              "brightness must be an integer from 0 to 100");
    }
    size_t patterns = sizeof PATTERNS / sizeof *PATTERNS;
    if (tas_arg_enum(args, "pattern", PATTERNS, patterns, &pattern) == TAS_ARG_BAD) {
        return tas_reply_fail(reply, "invalid_args", "pattern must be solid, blink or pulse");
    }
    if (tas_arg_int(args, "period_ms", 200, 5000, &period_ms) == TAS_ARG_BAD) {
        return tas_reply_fail(reply, "invalid_args",
                              "period_ms must be an integer from 200 to 5000");
    }
    int64_t index = -1;
    if (tas_arg_int(args, "index", 0, led->count - 1, &index) == TAS_ARG_BAD) {
        return tas_reply_fail(reply, "invalid_args",
                              "index must be an integer from 0 to the last LED");
    }
    look.brightness = (uint8_t)brightness;
    look.pattern = (tas_pattern_t)pattern;
    look.period_ms = (uint16_t)period_ms;
    claim(led, now_ms);
    for (size_t i = 0; i < led->count; i++) {
        if (index < 0 || (size_t)index == i) show(led, i, &look, now_ms);
    }
}

void tas_led_command(tas_led_t *led, const tas_msg_t *cmd, uint64_t now_ms, tas_msg_t *reply) {
    tas_str_t name = cmd->cmd.name;
    if (tas_str_eq(name, "set")) {
        set(led, cmd->cmd.args, now_ms, reply);
    } else if (tas_str_eq(name, "off")) {
        claim(led, now_ms);
        for (size_t i = 0; i < led->count; i++) show(led, i, &OFF, now_ms);
    } else if (tas_str_eq(name, "release")) {
        tas_led_release(led);
    } else {
        tas_reply_fail(reply, "unsupported", "led has no such command");
    }
}

void tas_led_release(tas_led_t *led) {
    led->claimed = false;
}

tas_rgb_t tas_led_rgb(const tas_led_t *led, size_t index, uint8_t brightness_cap, uint64_t now_ms) {
    if (!led->claimed || index >= led->count) return (tas_rgb_t){0};
    uint64_t since = led->since_ms[index];
    return tas_look_rgb(&led->looks[index], brightness_cap, now_ms > since ? now_ms - since : 0);
}
