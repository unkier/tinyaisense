#pragma once
// The Board layer: everything about one hardware model, declared in a single board file under
// boards/. The core and the Kind modules read it and never name a Board. Adding a Board means a
// board file, its Kconfig choice entry and CMake line, a boards/<board>.defaults and a PlatformIO
// env; the core and the Kind modules don't change.

#include <stddef.h>
#include <stdint.h>

#include "tas_dsp.h"

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
    void (*init)(void);         // pins the Board needs set before anything else runs
} tas_board_t;

// The Board this binary was built for.
const tas_board_t *tas_board(void);
