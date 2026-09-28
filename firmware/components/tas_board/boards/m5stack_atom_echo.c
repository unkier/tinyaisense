// M5Stack Atom Echo: ESP32-PICO-D4, SPM1423 PDM mic, NS4168 I2S amp, one SK6812, one button.
//
// The mic's PDM clock and the speaker's LRCK share GPIO33, so audio is half duplex: the mic and
// the speaker form an exclusive group.

#include "driver/gpio.h"
#include "tas_board.h"

#define PIN_I2S_BCLK 19
#define PIN_I2S_WS_PDM_CLK 33
#define PIN_I2S_DOUT 22
#define PIN_MIC_DIN 23
#define PIN_BUTTON 39
#define PIN_LED 27
#define PIN_G0 0

static const tas_cap_t caps[] = {
    {"mic", TAS_KIND_MIC},
    {"speaker", TAS_KIND_SPEAKER},
    {"button", TAS_KIND_BUTTON},
    {"led", TAS_KIND_LED},
};

static const char *const half_duplex[] = {"mic", "speaker"};

static const tas_group_t exclusive[] = {{half_duplex, 2}};

static void init(void) {
    // The CH552 otherwise holds G0 near 4 V, which costs WiFi sensitivity (M5Unified does this too).
    gpio_set_direction(PIN_G0, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_G0, 1);
}

static const tas_board_t board = {
    .name = "m5stack-atom-echo",
    .caps = caps,
    .cap_count = sizeof caps / sizeof *caps,
    .exclusive = exclusive,
    .exclusive_count = sizeof exclusive / sizeof *exclusive,
    .mic_cond = {.rate = 16000, .highpass_hz = 100, .gain_db = 15, .warmup_ms = 20},
    .spk_cond = {.rate = 16000, .lowcut_hz = 300},
    .led_brightness_cap = 64,
    .init = init,
};

const tas_board_t *tas_board(void) {
    return &board;
}
