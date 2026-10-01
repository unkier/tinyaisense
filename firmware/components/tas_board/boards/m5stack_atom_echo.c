// M5Stack Atom Echo: ESP32-PICO-D4, SPM1423 PDM mic, NS4168 I2S amp, one SK6812, one button.
//
// The mic's PDM clock and the speaker's LRCK share GPIO33, so audio is half duplex: the mic and
// the speaker form an exclusive group.

#include "driver/gpio.h"
#include "esp_log.h"
#include "led_strip.h"
#include "tas_board.h"

#define PIN_I2S_BCLK 19
#define PIN_I2S_WS_PDM_CLK 33
#define PIN_I2S_DOUT 22
#define PIN_MIC_DIN 23
#define PIN_BUTTON 39
#define PIN_LED 27
#define PIN_G0 0

static const tas_cap_t caps[] = {
    {.id = "mic", .kind = TAS_KIND_MIC},
    {.id = "speaker", .kind = TAS_KIND_SPEAKER},
    {.id = "button", .kind = TAS_KIND_BUTTON},
    {.id = "led", .kind = TAS_KIND_LED, .count = 1},
};

static const char *const half_duplex[] = {"mic", "speaker"};

static const tas_group_t exclusive[] = {{half_duplex, 2}};

#define WHITE {255, 255, 255}
#define RED {255, 0, 0}
#define AMBER {255, 160, 0}

// The Simulator renders the same looks (tinyaisense_sim/led.py).
static const tas_look_t status_looks[] = {
    [TAS_STATUS_IDENTIFY] = {WHITE, 100, TAS_PATTERN_BLINK, 250},
    [TAS_STATUS_LISTENING] = {{0, 255, 0}, 100, TAS_PATTERN_SOLID, 0},
    [TAS_STATUS_OFFLINE] = {RED, 100, TAS_PATTERN_BLINK, 1000},
    [TAS_STATUS_CONNECTING] = {AMBER, 100, TAS_PATTERN_BLINK, 500},
    [TAS_STATUS_PENDING] = {{255, 0, 255}, 100, TAS_PATTERN_PULSE, 2000},
    [TAS_STATUS_NO_CONFIG] = {RED, 100, TAS_PATTERN_SOLID, 0},
    [TAS_STATUS_SPEAKING] = {{0, 0, 255}, 100, TAS_PATTERN_SOLID, 0},
    [TAS_STATUS_THINKING] = {{0, 255, 255}, 100, TAS_PATTERN_PULSE, 1000},
    [TAS_STATUS_NO_AGENT] = {AMBER, 20, TAS_PATTERN_SOLID, 0},
    [TAS_STATUS_AGENT] = {{0, 0, 0}, 0, TAS_PATTERN_SOLID, 0},
};

static led_strip_handle_t strip;

static void init(void) {
    // The CH552 otherwise holds G0 near 4 V, which costs WiFi sensitivity. M5Unified does this too.
    gpio_set_direction(PIN_G0, GPIO_MODE_OUTPUT);
    gpio_set_level(PIN_G0, 1);
    // The button has an external pull-up; GPIO39 has no internal one.
    gpio_config_t button = {.pin_bit_mask = 1ULL << PIN_BUTTON, .mode = GPIO_MODE_INPUT};
    ESP_ERROR_CHECK(gpio_config(&button));
    led_strip_config_t led = {
        .strip_gpio_num = PIN_LED,
        .max_leds = 1,
        .led_model = LED_MODEL_SK6812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
    };
    led_strip_rmt_config_t rmt = {.clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 10000000};
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&led, &rmt, &strip));
    ESP_ERROR_CHECK(led_strip_clear(strip));
}

static void led_set(const tas_cap_t *cap, size_t index, tas_rgb_t rgb) {
    (void)cap;
    led_strip_set_pixel(strip, index, rgb.r, rgb.g, rgb.b);
}

static void led_show(const tas_cap_t *cap) {
    (void)cap;
    esp_err_t err = led_strip_refresh(strip);
    if (err) ESP_LOGW("board", "LED refresh failed: %s", esp_err_to_name(err));
}

static bool reset_button_down(void) {
    return gpio_get_level(PIN_BUTTON) == 0;
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
    .status_led = "led",
    .status_looks = status_looks,
    .init = init,
    .led_set = led_set,
    .led_show = led_show,
    .reset_button_down = reset_button_down,
};

const tas_board_t *tas_board(void) {
    return &board;
}
