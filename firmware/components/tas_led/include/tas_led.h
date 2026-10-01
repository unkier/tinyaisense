#pragma once
// The led Kind (docs/protocol/kinds/led.md) on one led capability, pure C: the agent's claim on
// its LEDs, what `set`, `off` and `release` do to it, and the color each LED shows under it.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tas_status.h"
#include "tas_wire.h"

#define TAS_LED_MAX 16 // the most LEDs one capability may have

typedef struct {
    uint16_t count;
    bool claimed;
    tas_look_t looks[TAS_LED_MAX];  // what each LED shows under the claim
    uint64_t since_ms[TAS_LED_MAX]; // when each look was set: its blink or pulse starts there
} tas_led_t;

// A capability of `count` LEDs, unclaimed. Past TAS_LED_MAX, only the first TAS_LED_MAX work.
void tas_led_init(tas_led_t *led, uint16_t count);

// Runs the led command `cmd` at `now_ms`. `reply` comes in ok; a command that breaks the contract
// fails it with invalid_args, and one the Kind doesn't have with unsupported.
void tas_led_command(tas_led_t *led, const tas_msg_t *cmd, uint64_t now_ms, tas_msg_t *reply);

// Ends the claim, as the end of the Connection does.
void tas_led_release(tas_led_t *led);

// The color LED `index` shows under the claim at `now_ms`, brightness 100 being `brightness_cap`
// (0-255). Off with no claim.
tas_rgb_t tas_led_rgb(const tas_led_t *led, size_t index, uint8_t brightness_cap, uint64_t now_ms);
