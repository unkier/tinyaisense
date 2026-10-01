#pragma once
// The Status (docs/protocol/kinds/device.md#status): the layers that apply now, and the one the
// device shows. Plus the looks and patterns that the Status and the led Kind animate with. Pure C.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Highest priority first.
typedef enum {
    TAS_STATUS_IDENTIFY,
    TAS_STATUS_LISTENING,
    TAS_STATUS_CLAIM, // an agent's LED claim: the LED shows what the agent set
    TAS_STATUS_OFFLINE,
    TAS_STATUS_CONNECTING,
    TAS_STATUS_PENDING,
    TAS_STATUS_NO_CONFIG,
    TAS_STATUS_SPEAKING,
    TAS_STATUS_THINKING,
    TAS_STATUS_NO_AGENT,
    TAS_STATUS_AGENT,
} tas_status_t;

typedef struct {
    bool identify;
    bool listening; // a mic stream is open
    bool claimed;   // an agent has claimed the Status LED
    bool offline;   // no WiFi
    bool connecting; // WiFi up, no hub yet
    bool pending;
    bool no_config; // no WiFi credentials
    bool speaking;  // a speaker stream is open
    tas_status_t hub; // the hub layer: TAS_STATUS_THINKING, _NO_AGENT or _AGENT
} tas_status_layers_t;

// The highest-priority state that applies.
tas_status_t tas_status_shown(const tas_status_layers_t *layers);

// The hub layer that `status{state}` names. False if `state` isn't one.
bool tas_status_hub_layer(const char *state, size_t len, tas_status_t *layer);

// The state's name, as device.md writes it.
const char *tas_status_name(tas_status_t status);

typedef enum {
    TAS_PATTERN_SOLID,
    TAS_PATTERN_BLINK, // on for the first half of each period, off for the second
    TAS_PATTERN_PULSE, // fades up from dark to full at half the period, and back down
} tas_pattern_t;

// The pattern's level at `t_ms`, 0-255.
uint8_t tas_pattern_level(tas_pattern_t pattern, uint32_t period_ms, uint64_t t_ms);

typedef struct {
    uint8_t r, g, b;
} tas_rgb_t;

// How an LED looks: what led.set takes.
typedef struct {
    tas_rgb_t color;
    uint8_t brightness; // 0-100, scaled by the Board's cap
    tas_pattern_t pattern;
    uint16_t period_ms;
} tas_look_t;

// The color `look` gives an LED at `t_ms` into its pattern, its brightness scaled so that 100 is
// `brightness_cap` (0-255).
tas_rgb_t tas_look_rgb(const tas_look_t *look, uint8_t brightness_cap, uint64_t t_ms);
