#pragma once
// The Board layer: everything about one hardware model, declared in a single board file under
// boards/. The core and the Kind modules read it and never name a Board. Adding a Board means a
// board file, its Kconfig choice entry and CMake line, a boards/<board>.defaults and PlatformIO
// envs (release and debug); the core and the Kind modules don't change.

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

// From the speaker's I2S ISR, as the DMA finishes a descriptor and moves on to the next: `dma_buf`
// is the one that finished. Returns whether it woke a task that should run now.
typedef bool (*tas_speaker_sent_t)(void *ctx, void *dma_buf);

// How a speaker plays: an I2S TX channel looping a DMA ring of interleaved s16 at spk_cond's rate,
// which zeroes each descriptor once it has played. The speaker module fills the ring, and cuts it
// in place.
typedef struct {
    size_t descs, frames, channels; // the ring: descs descriptors of frames frames each
    // Frames of the next descriptor the DMA may have fetched before the last one's on_sent, with
    // margin: a cut leaves them alone.
    size_t fetch_lead;
    // Registers `sent`. Call it once, before the first start.
    void (*open)(tas_speaker_sent_t sent, void *ctx);
    // Zeroes the ring and starts the DMA at its first descriptor.
    void (*start)(void);
    // Stops the DMA at once, wherever it is. Over audio this clicks.
    void (*stop)(void);
    // Copies one descriptor of PCM into the ring's next free one: the next to play when nothing
    // is queued. False if none is free. Never blocks.
    bool (*write)(const int16_t *pcm);
} tas_speaker_hw_t;

// How a mic records: an I2S RX channel of mono s16 at mic_cond's rate, which runs only while the
// mic streams. One task at a time drives it.
typedef struct {
    void (*start)(void); // starts the channel, with nothing captured yet
    void (*stop)(void);
    // Reads the next `samples` samples captured, waiting up to `timeout_ms` for the DMA to
    // capture them. False if it timed out.
    bool (*read)(int16_t *pcm, size_t samples, uint32_t timeout_ms);
} tas_mic_hw_t;

// One capability: how it appears in the Manifest, and the hardware behind it.
typedef struct {
    const char *id; // unique on the device, e.g. "speaker"
    tas_kind_t kind;
    uint16_t count;                  // led: how many LEDs it has
    const tas_speaker_hw_t *speaker; // speaker: its hardware
    const tas_mic_hw_t *mic;         // mic: its hardware
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
    // Hands what an exclusive group's capabilities share (e.g. a pin) to `cap`. Its peers must
    // be idle first. NULL if the Board has no exclusive groups.
    void (*exclusive_switch)(const tas_cap_t *cap);
    // Buffers one LED's color, already scaled; led_show sends a capability's LEDs out.
    void (*led_set)(const tas_cap_t *cap, size_t index, tas_rgb_t rgb);
    void (*led_show)(const tas_cap_t *cap);
    bool (*reset_button_down)(void); // whether the factory-reset button is held now
} tas_board_t;

// The Board this binary was built for.
const tas_board_t *tas_board(void);
