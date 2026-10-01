// M5Stack Atom Echo: ESP32-PICO-D4, SPM1423 PDM mic, NS4168 I2S amp, one SK6812, one button.
//
// The mic's PDM clock and the speaker's LRCK share GPIO33, so audio is half duplex: the mic and
// the speaker form an exclusive group. Both I2S channels stay allocated, and switching between
// them only re-routes GPIO33 in the GPIO matrix (~0.1 ms).

#include "driver/gpio.h"
#include "driver/i2s_pdm.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "led_strip.h"
#include "soc/i2s_periph.h"
#include "tas_board.h"

#define PIN_I2S_BCLK 19
#define PIN_I2S_WS_PDM_CLK 33
#define PIN_I2S_DOUT 22
#define PIN_MIC_DIN 23
#define PIN_BUTTON 39
#define PIN_LED 27
#define PIN_G0 0

#define RATE 16000
#define I2S_MIC I2S_NUM_0 // PDM to PCM exists only on I2S0
#define I2S_SPK I2S_NUM_1
// The speaker's ring: 8 descriptors of 2.5 ms, so a cut is silent within about one plus a 2 ms
// ramp. The DMA moves on ~0.25 ms (4 frames) before on_sent runs; the lead doubles that.
#define SPK_DESCS 8
#define SPK_FRAMES 40
#define SPK_FETCH_LEAD 8
#define SPK_CHANNELS 2 // the NS4168 plays stereo L=R; ESP32's swapped 16-bit slots don't matter
#define MIC_DESCS 6
#define MIC_FRAMES 160

static i2s_chan_handle_t spk_chan, mic_chan;
static tas_speaker_sent_t spk_sent;
static void *spk_ctx;

static bool on_sent(i2s_chan_handle_t chan, i2s_event_data_t *event, void *ctx) {
    (void)chan;
    (void)ctx;
    return spk_sent(spk_ctx, event->dma_buf);
}

static void speaker_open(tas_speaker_sent_t sent, void *ctx) {
    spk_sent = sent;
    spk_ctx = ctx;
    i2s_event_callbacks_t callbacks = {.on_sent = on_sent};
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(spk_chan, &callbacks, NULL));
}

static void speaker_start(void) {
    // Preloading starts at the first descriptor; the whole ring leaves no stale audio behind.
    static const int16_t silence[SPK_FRAMES * SPK_CHANNELS];
    for (size_t i = 0; i < SPK_DESCS; i++) {
        size_t loaded;
        ESP_ERROR_CHECK(i2s_channel_preload_data(spk_chan, silence, sizeof silence, &loaded));
    }
    ESP_ERROR_CHECK(i2s_channel_enable(spk_chan));
}

static void speaker_stop(void) {
    ESP_ERROR_CHECK(i2s_channel_disable(spk_chan));
}

static bool speaker_write(const int16_t *pcm) {
    size_t written;
    size_t size = SPK_FRAMES * SPK_CHANNELS * sizeof *pcm;
    return i2s_channel_write(spk_chan, pcm, size, &written, 0) == ESP_OK;
}

static void mic_start(void) {
    ESP_ERROR_CHECK(i2s_channel_enable(mic_chan));
}

static void mic_stop(void) {
    ESP_ERROR_CHECK(i2s_channel_disable(mic_chan));
}

static const tas_speaker_hw_t speaker = {
    .descs = SPK_DESCS,
    .frames = SPK_FRAMES,
    .channels = SPK_CHANNELS,
    .fetch_lead = SPK_FETCH_LEAD,
    .open = speaker_open,
    .start = speaker_start,
    .stop = speaker_stop,
    .write = speaker_write,
};

static const tas_mic_hw_t mic = {.start = mic_start, .stop = mic_stop};

static const tas_cap_t caps[] = {
    {.id = "mic", .kind = TAS_KIND_MIC, .mic = &mic},
    {.id = "speaker", .kind = TAS_KIND_SPEAKER, .speaker = &speaker},
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

static void mic_init(void) {
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_MIC, I2S_ROLE_MASTER);
    chan.dma_desc_num = MIC_DESCS;
    chan.dma_frame_num = MIC_FRAMES;
    ESP_ERROR_CHECK(i2s_new_channel(&chan, NULL, &mic_chan));
    i2s_pdm_rx_config_t pdm = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.clk = PIN_I2S_WS_PDM_CLK, .din = PIN_MIC_DIN},
    };
    // A 2.048 MHz PDM clock, and the SPM1423's data in the right slot, as the loopback spike ran.
    pdm.clk_cfg.dn_sample_mode = I2S_PDM_DSR_16S;
    pdm.slot_cfg.slot_mask = I2S_PDM_SLOT_RIGHT;
    ESP_ERROR_CHECK(i2s_channel_init_pdm_rx_mode(mic_chan, &pdm));
}

static void speaker_init(void) {
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(I2S_SPK, I2S_ROLE_MASTER);
    chan.dma_desc_num = SPK_DESCS;
    chan.dma_frame_num = SPK_FRAMES;
    chan.auto_clear_after_cb = true; // a descriptor nobody refills plays silence
    ESP_ERROR_CHECK(i2s_new_channel(&chan, &spk_chan, NULL));
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg =
            I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED,
                     .bclk = PIN_I2S_BCLK,
                     .ws = PIN_I2S_WS_PDM_CLK,
                     .dout = PIN_I2S_DOUT,
                     .din = I2S_GPIO_UNUSED},
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(spk_chan, &std));
}

// GPIO33 carries the speaker's LRCK or the mic's PDM clock, whichever channel has it.
static void exclusive_switch(const tas_cap_t *cap) {
    uint32_t signal = cap->kind == TAS_KIND_MIC ? i2s_periph_signal[I2S_MIC].m_rx_ws_sig
                                                : i2s_periph_signal[I2S_SPK].m_tx_ws_sig;
    esp_rom_gpio_connect_out_signal(PIN_I2S_WS_PDM_CLK, signal, false, false);
}

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
    // The speaker last, so GPIO33 starts out routed to it.
    mic_init();
    speaker_init();
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
    .mic_cond = {.rate = RATE, .highpass_hz = 100, .gain_db = 15, .warmup_ms = 20},
    .spk_cond = {.rate = RATE, .lowcut_hz = 300},
    .led_brightness_cap = 64,
    .status_led = "led",
    .status_looks = status_looks,
    .init = init,
    .exclusive_switch = exclusive_switch,
    .led_set = led_set,
    .led_show = led_show,
    .reset_button_down = reset_button_down,
};

const tas_board_t *tas_board(void) {
    return &board;
}
