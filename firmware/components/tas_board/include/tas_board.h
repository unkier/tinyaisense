#pragma once
// The Board layer: everything about one hardware model, declared in a single board file under
// boards/. The core and the Kind modules read it and never name a Board. Adding a Board means a
// board file, its Kconfig choice entry and CMake line, a boards/<board>.defaults and a PlatformIO
// env; the core and the Kind modules don't change.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tas_dsp.h"
#include "tas_status.h"

typedef enum {
    TAS_KIND_MIC,
    TAS_KIND_SPEAKER,
    TAS_KIND_BUTTON,
    TAS_KIND_LED,
} tas_kind_t;

// One capability, as it appears in the Manifest.
typedef struct {
    const char *id; // unique on the device, e.g. "speaker"
    tas_kind_t kind;
    uint16_t count; // led: how many LEDs it has
} tas_cap_t;

// Capabilities of which at most one is active at a time, e.g. a half-duplex mic and speaker.
typedef struct {
    const char *const *ids;
    size_t count;
} tas_group_t;

typedef struct {
    const char *name; // the Board id in the Manifest
    const tas_cap_t *caps;
    size_t cap_count;
    const tas_group_t *exclusive;
    size_t exclusive_count;
    tas_mic_cond_config_t mic_cond;
    tas_spk_cond_config_t spk_cond;
    uint8_t led_brightness_cap; // what LED brightness 100 maps to, 0-255
    const char *status_led;     // the led capability that shows the Status
    const tas_look_t *status_looks; // how it shows each tas_status_t, bar TAS_STATUS_CLAIM
    void (*init)(void);         // pins and drivers, set up before anything else runs
    // Buffers one LED's color, already scaled; led_show sends a capability's LEDs out.
    void (*led_set)(const tas_cap_t *cap, size_t index, tas_rgb_t rgb);
    void (*led_show)(const tas_cap_t *cap);
    bool (*reset_button_down)(void); // whether the factory-reset button is held now
} tas_board_t;

// The Board this binary was built for.
const tas_board_t *tas_board(void);
