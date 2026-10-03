// PROTOTYPE, throwaway: the full-duplex I2S spike on the ReSpeaker Lite + XIAO ESP32S3, for #59.
// Not the firmware. A console on the USB-Serial/JTAG drives the tests (`help`); host/spike.py
// runs them with the acoustic rig.
//
// The XU316 is the I2S master (48 kHz, Philips, 32-bit stereo slots); the S3 runs one controller
// as a full-duplex slave. TX and RX stay enabled from boot, so a TX frame and the RX frame on the
// line at the same instant keep a fixed offset in frames. `loop on` routes GPIO43's pad (the
// S3's DOUT, which also feeds the AIC3204) into the RX data input, so RX reads back what TX put
// on the line: that offset, the DMA's fetch lead and the cut's timing, all on the line itself.

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/pulse_cnt.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp32s3/rom/rtc.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_rom_gpio.h"
#include "esp_rom_sys.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"
#include "soc/i2s_periph.h"
#include "soc/rtc_cntl_reg.h"
#include "tas_cut.h"

#include "secrets.h"

#define PIN_XRST 2
#define PIN_SDA 5
#define PIN_SCL 6
#define PIN_WS 7
#define PIN_BCLK 8
#define PIN_MCLK 9
#define PIN_DOUT 43
#define PIN_DIN 44

#define RATE 48000
#define DESCS 8
#define FRAMES 120 // 2.5 ms per descriptor
#define RAMP 96    // 2 ms
#define MAXSEQ 8192 // descriptor stamps kept per run: 20 s
#define CAP_MAX (16 * RATE) // capture frames: 16 s of 32-bit stereo, 6 MB of PSRAM
#define LOAD_MAX (16 * RATE) // loaded samples: 16 s of s16 mono
#define STAMP_RING 64
#define UDP_PORT 9000
#define UDP_FRAME 320 // 20 ms at 16 kHz
#define MARK 0x12345600 // a loop-mode marker sample, exact on the line
#define LOUD (1 << 21)  // -60 dBFS in a 32-bit slot

static i2s_chan_handle_t tx, rx;
static tas_cut_t cut;
static TaskHandle_t tx_task_h;
static volatile bool i2s_ready;
static int64_t t_app;

// Counted in the ISRs.
static volatile uint32_t tx_sent, rx_recv, tx_qovf, rx_qovf;
static volatile bool stamp_on;
static volatile uint32_t tx_base, rx_base;
static uint32_t tx_t[MAXSEQ], rx_t[MAXSEQ];   // run stamps, esp_timer us (low 32 bits)
static volatile int64_t rx_ring_t[STAMP_RING]; // each RX descriptor's completion, by seq

// The cut, done in the TX task.
static volatile bool cut_req, cut_ok;
static volatile int64_t cut_us, cut_reach_us;

// What TX plays.
typedef enum { SRC_SILENCE, SRC_TONE, SRC_BUF, SRC_UDP } src_t;
static volatile src_t src = SRC_SILENCE;
static volatile float tone_hz = 1000, tone_amp = 0.25f;
static float tone_phase;
static int16_t *load_buf;
static size_t load_len, buf_pos;
static volatile float sw_gain = 1.0f;
static volatile int64_t tx_start_frame = -1; // the TX frame where the loaded buffer started
static uint32_t tx_writes, tx_short;

// RX.
static int64_t rx_frames; // read since enable
static uint32_t rx_short;
static volatile bool loop_on;
static struct {
    int32_t *buf;
    size_t max, n;
    volatile bool on;
    int64_t frame0;
} cap;
static struct {
    volatile size_t target, n;
    double sum[2];
    int32_t peak[2];
} lvl;
static volatile bool audio_watch;
static volatile int64_t audio_at;
static volatile int64_t loud_frame = -1, loud_us; // loop mode: the last loud frame on the line
static volatile uint32_t marks_seen;
static volatile int64_t mark_frame;

// UDP full duplex at 16 kHz.
static int udp_sock = -1;
static struct sockaddr_in udp_peer;
static volatile bool udp_on;
#define SPK16 8192
static int16_t spk16[SPK16];
static volatile size_t spk16_head, spk16_tail; // udp_rx writes head, TX reads tail
static volatile bool spk16_primed;
static uint32_t udp_in, udp_out, udp_under, udp_over;

// The boot.
static int64_t boot_ws_us = -1, boot_i2c_us = -1;
static bool boot_ws_already;

static pcnt_unit_handle_t pc_ws, pc_bclk, pc_mclk;
static i2c_master_bus_handle_t i2c_bus;
static i2c_master_dev_handle_t dev_x, dev_a;

static int64_t now_us(void) {
    return esp_timer_get_time();
}

// ---- I2S ----

static bool on_sent(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    int64_t now = now_us();
    uint32_t s = tx_sent++;
    if (stamp_on && s - tx_base < MAXSEQ) tx_t[s - tx_base] = (uint32_t)now;
    if (tas_cut_sent(&cut, e->dma_buf, now)) cut_reach_us = now;
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(tx_task_h, &woken);
    return woken == pdTRUE;
}

static bool on_recv(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    int64_t now = now_us();
    uint32_t s = rx_recv++;
    rx_ring_t[s % STAMP_RING] = now;
    if (stamp_on && s - rx_base < MAXSEQ) rx_t[s - rx_base] = (uint32_t)now;
    return false;
}

static bool on_send_q_ovf(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    tx_qovf++;
    return false;
}

static bool on_recv_q_ovf(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    rx_qovf++;
    return false;
}

static void i2s_init(void) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    cc.dma_desc_num = DESCS;
    cc.dma_frame_num = FRAMES;
    cc.auto_clear_after_cb = true; // a descriptor nobody refills plays silence
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &tx, &rx));
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg =
            I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = I2S_GPIO_UNUSED,
                     .bclk = PIN_BCLK,
                     .ws = PIN_WS,
                     .dout = PIN_DOUT,
                     .din = PIN_DIN},
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &std));
    i2s_event_callbacks_t tcb = {.on_sent = on_sent, .on_send_q_ovf = on_send_q_ovf};
    i2s_event_callbacks_t rcb = {.on_recv = on_recv, .on_recv_q_ovf = on_recv_q_ovf};
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(tx, &tcb, NULL));
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(rx, &rcb, NULL));

    tas_cut_config_t ccfg = {.descs = DESCS,
                             .frames = FRAMES,
                             .channels = 4, // s16 halves of 32-bit stereo: audio in the high ones
                             .rate = RATE,
                             .ramp = RAMP,
                             .fetch_lead = 24};
    if (!tas_cut_init(&cut, &ccfg)) abort();
    static const int32_t silence[FRAMES * 2];
    for (int i = 0; i < DESCS; i++) {
        size_t loaded;
        ESP_ERROR_CHECK(i2s_channel_preload_data(tx, silence, sizeof silence, &loaded));
    }
    tas_cut_restart(&cut, now_us());
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));
    i2s_ready = true;
}

static void set_loop(bool on) {
    if (on) {
        gpio_input_enable(PIN_DOUT);
        esp_rom_gpio_connect_in_signal(PIN_DOUT, i2s_periph_signal[0].data_in_sig, false);
    } else {
        esp_rom_gpio_connect_in_signal(PIN_DIN, i2s_periph_signal[0].data_in_sig, false);
    }
    loop_on = on;
}

// ---- TX ----

#define UP_TAPS 24 // per phase: a 72-tap 3x interpolator, like the XU316's
static float up_h[3][UP_TAPS];
static float up_hist[UP_TAPS];
static size_t up_pos;

static void up_init(void) {
    const int n = 3 * UP_TAPS;
    const double fc = 7600.0 / RATE;
    for (int i = 0; i < n; i++) {
        double x = i - (n - 1) / 2.0;
        double sinc = x == 0 ? 2 * fc : sin(2 * M_PI * fc * x) / (M_PI * x);
        double w = 0.42 - 0.5 * cos(2 * M_PI * i / (n - 1)) + 0.08 * cos(4 * M_PI * i / (n - 1));
        up_h[i % 3][i / 3] = (float)(3 * sinc * w);
    }
}

static size_t spk16_count(void) {
    return (spk16_head - spk16_tail) % SPK16;
}

// FRAMES at 48 kHz from FRAMES / 3 at 16 kHz out of the UDP queue.
static void upsample(float *out) {
    if (!spk16_primed && spk16_count() >= 2 * UDP_FRAME) spk16_primed = true;
    for (int m = 0; m < FRAMES / 3; m++) {
        float x = 0;
        if (spk16_primed && spk16_count()) {
            x = spk16[spk16_tail];
            spk16_tail = (spk16_tail + 1) % SPK16;
        } else if (spk16_primed) {
            spk16_primed = false;
            udp_under++;
        }
        up_pos = (up_pos + 1) % UP_TAPS;
        up_hist[up_pos] = x;
        for (int p = 0; p < 3; p++) {
            float acc = 0;
            for (int k = 0; k < UP_TAPS; k++) acc += up_h[p][k] * up_hist[(up_pos + UP_TAPS - k) % UP_TAPS];
            out[3 * m + p] = acc;
        }
    }
}

// One descriptor of what TX plays, as 32-bit stereo.
static void build(int32_t *out, bool *starts) {
    float pcm[FRAMES];
    *starts = false;
    switch (src) {
    case SRC_SILENCE:
        memset(pcm, 0, sizeof pcm);
        break;
    case SRC_TONE: {
        float step = 2 * (float)M_PI * tone_hz / RATE, amp = tone_amp * 32767;
        for (int i = 0; i < FRAMES; i++) {
            pcm[i] = amp * sinf(tone_phase);
            tone_phase += step;
            if (tone_phase > 2 * (float)M_PI) tone_phase -= 2 * (float)M_PI;
        }
        break;
    }
    case SRC_BUF:
        if (buf_pos == 0) *starts = true;
        for (int i = 0; i < FRAMES; i++) pcm[i] = buf_pos + i < load_len ? load_buf[buf_pos + i] : 0;
        buf_pos += FRAMES;
        if (buf_pos >= load_len) src = SRC_SILENCE;
        break;
    case SRC_UDP:
        upsample(pcm);
        break;
    }
    float g = sw_gain * 65536.0f; // the 32-bit slot keeps the resolution a low gain takes off
    for (int i = 0; i < FRAMES; i++) {
        float v = pcm[i] * g;
        int32_t s = v >= 2147483520.0f ? INT32_MAX : v <= -2147483520.0f ? INT32_MIN : (int32_t)lrintf(v);
        out[2 * i] = out[2 * i + 1] = s;
    }
}

// The lead scan: a marker written into the descriptor playing, at the model's position plus
// `lead_m` frames, `lead_d_us` after its predecessor's on_sent. It plays only if the DMA hadn't
// fetched that far yet.
static volatile bool lead_req;
static volatile int lead_m, lead_d_us;
static volatile int lead_pos; // where it went, or -1

static void lead_trial(void) {
    uint32_t sents = cut.sents;
    while (cut.sents == sents) {
    }
    size_t last = cut.last;
    int64_t t = cut.sent_us[last];
    while (now_us() < t + lead_d_us) {
    }
    int64_t now = now_us();
    int pos = (int)((now - t) * RATE / 1000000) + lead_m;
    lead_pos = -1;
    if (pos >= 0 && pos + 2 <= FRAMES && cut.known == DESCS) {
        int32_t *buf = (int32_t *)cut.ring[(last + 1) % DESCS];
        for (int i = pos; i < pos + 2; i++) buf[2 * i] = buf[2 * i + 1] = MARK;
        lead_pos = pos;
    }
}

static void tx_task(void *arg) {
    static int32_t out[FRAMES * 2];
    bool pending = false, starts = false;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(50));
        if (!i2s_ready) continue;
        if (cut_req) {
            src = SRC_SILENCE;
            pending = false;
            int64_t now = now_us();
            cut_ok = tas_cut_patch(&cut, now);
            cut_us = now;
            cut_req = false;
        }
        if (lead_req) {
            lead_trial();
            lead_req = false;
        }
        for (;;) {
            if (!pending) {
                build(out, &starts);
                pending = true;
            }
            size_t w = 0;
            esp_err_t err = i2s_channel_write(tx, out, sizeof out, &w, 0);
            if (w && w != sizeof out) tx_short++;
            if (err != ESP_OK || w != sizeof out) break;
            if (starts) tx_start_frame = (int64_t)(DESCS + tx_writes) * FRAMES;
            tx_writes++;
            pending = starts = false;
        }
    }
}

// ---- RX ----

static void rx_task(void *arg) {
    static int32_t in[FRAMES * 2];
    static int16_t mic16[UDP_FRAME];
    static size_t mic16_n;
    for (;;) {
        size_t r = 0;
        i2s_channel_read(rx, in, sizeof in, &r, pdMS_TO_TICKS(100));
        if (r != sizeof in) {
            if (r) rx_short++;
            if (!r) continue;
        }
        size_t n = r / 8;
        int64_t frame0 = rx_frames;
        int64_t desc_t = rx_ring_t[(uint32_t)(frame0 / FRAMES) % STAMP_RING];
        if (lvl.n < lvl.target) {
            for (size_t i = 0; i < n; i++) {
                for (int c = 0; c < 2; c++) {
                    int32_t s = in[2 * i + c];
                    double d = s / 2147483648.0;
                    lvl.sum[c] += d * d;
                    int32_t a = s == INT32_MIN ? INT32_MAX : abs(s);
                    if (a > lvl.peak[c]) lvl.peak[c] = a;
                }
            }
            lvl.n += n;
        }
        if (audio_watch) {
            for (size_t i = 0; i < n; i++) {
                if (abs(in[2 * i] >> 16) > 1) {
                    audio_at = now_us();
                    audio_watch = false;
                    break;
                }
            }
        }
        if (loop_on) {
            for (size_t i = 0; i < n; i++) {
                int32_t s = in[2 * i];
                if (s == MARK) {
                    marks_seen++;
                    mark_frame = frame0 + i;
                }
                if (s > LOUD || s < -LOUD) {
                    loud_frame = frame0 + i;
                    loud_us = desc_t - (int64_t)(FRAMES - 1 - i) * 1000000 / RATE;
                }
            }
        }
        if (cap.on) {
            if (cap.n == 0) cap.frame0 = frame0;
            size_t take = n < cap.max - cap.n ? n : cap.max - cap.n;
            memcpy(cap.buf + 2 * cap.n, in, take * 8);
            cap.n += take;
            if (cap.n == cap.max) cap.on = false;
        }
        if (udp_on) {
            for (size_t i = 0; i < n; i++) {
                if ((frame0 + i) % 3) continue; // every 3rd: the XU316 band-limited it to 8 kHz
                mic16[mic16_n++] = (int16_t)(in[2 * i] >> 16);
                if (mic16_n == UDP_FRAME) {
                    if (sendto(udp_sock, mic16, sizeof mic16, 0, (struct sockaddr *)&udp_peer,
                               sizeof udp_peer) == sizeof mic16) {
                        udp_out++;
                    }
                    mic16_n = 0;
                }
            }
        }
        rx_frames += n;
    }
}

// ---- PCNT, I2C ----

static pcnt_unit_handle_t pcnt_on(int gpio) {
    pcnt_unit_config_t ucfg = {.low_limit = -1, .high_limit = 32767};
    pcnt_unit_handle_t u;
    ESP_ERROR_CHECK(pcnt_new_unit(&ucfg, &u));
    pcnt_chan_config_t ccfg = {.edge_gpio_num = gpio, .level_gpio_num = -1};
    pcnt_channel_handle_t c;
    ESP_ERROR_CHECK(pcnt_new_channel(u, &ccfg, &c));
    ESP_ERROR_CHECK(pcnt_channel_set_edge_action(c, PCNT_CHANNEL_EDGE_ACTION_INCREASE,
                                                 PCNT_CHANNEL_EDGE_ACTION_HOLD));
    ESP_ERROR_CHECK(pcnt_unit_enable(u));
    ESP_ERROR_CHECK(pcnt_unit_start(u));
    gpio_pullup_dis(gpio); // pcnt_new_channel pulls it up; it's the XU316's
    return u;
}

// Rising edges per second on `u`, counted over `us`.
static double edge_hz(pcnt_unit_handle_t u, uint32_t us) {
    int c = 0;
    pcnt_unit_clear_count(u);
    int64_t t0 = now_us();
    esp_rom_delay_us(us);
    pcnt_unit_get_count(u, &c);
    int64_t t1 = now_us();
    return c * 1e6 / (double)(t1 - t0);
}

static bool ws_running(void) {
    return edge_hz(pc_ws, 1000) > 40000;
}

static void i2c_init(void) {
    i2c_master_bus_config_t bcfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_SDA,
        .scl_io_num = PIN_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bcfg, &i2c_bus));
    i2c_device_config_t x = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x42,
                             .scl_speed_hz = 100000};
    i2c_device_config_t a = {.dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x18,
                             .scl_speed_hz = 100000};
    ESP_ERROR_CHECK(i2c_master_bus_add_device(i2c_bus, &x, &dev_x));
    ESP_ERROR_CHECK(i2c_master_bus_add_device(i2c_bus, &a, &dev_a));
    esp_log_level_set("i2c.master", ESP_LOG_NONE); // a NACK while the XU316 boots is expected
}

// An XMOS control read: [resid, cmd|0x80, n+1], then n+1 bytes, the first the status.
static esp_err_t xmos_read(uint8_t resid, uint8_t cmd, uint8_t *out, size_t n) {
    uint8_t req[3] = {resid, (uint8_t)(cmd | 0x80), (uint8_t)(n + 1)};
    uint8_t buf[16];
    for (int tries = 0; tries < 50; tries++) {
        esp_err_t err = i2c_master_transmit(dev_x, req, 3, 20);
        if (err == ESP_OK) err = i2c_master_receive(dev_x, buf, n + 1, 20);
        if (err != ESP_OK) return err;
        if (buf[0] == 0) {
            memcpy(out, buf + 1, n);
            return ESP_OK;
        }
        if (buf[0] != 1) return ESP_ERR_INVALID_RESPONSE;
        vTaskDelay(1);
    }
    return ESP_ERR_TIMEOUT;
}

static esp_err_t xmos_write(uint8_t resid, uint8_t cmd, const uint8_t *data, size_t n) {
    uint8_t buf[16] = {resid, cmd, (uint8_t)n};
    memcpy(buf + 3, data, n);
    return i2c_master_transmit(dev_x, buf, 3 + n, 20);
}

static esp_err_t aic_read(uint8_t page, uint8_t reg, uint8_t *val) {
    uint8_t p[2] = {0, page};
    esp_err_t err = i2c_master_transmit(dev_a, p, 2, 20);
    if (err == ESP_OK) err = i2c_master_transmit_receive(dev_a, &reg, 1, val, 1, 20);
    if (page) {
        p[1] = 0;
        i2c_master_transmit(dev_a, p, 2, 20);
    }
    return err;
}

static esp_err_t aic_write(uint8_t page, uint8_t reg, uint8_t val) {
    uint8_t p[2] = {0, page}, w[2] = {reg, val};
    esp_err_t err = i2c_master_transmit(dev_a, p, 2, 20);
    if (err == ESP_OK) err = i2c_master_transmit(dev_a, w, 2, 20);
    if (page) {
        p[1] = 0;
        i2c_master_transmit(dev_a, p, 2, 20);
    }
    return err;
}

// ---- The XU316's boot ----

// From `t0`: WS running, I2C answering, the mic's first non-zero sample. The XU316 masters the
// bus while it boots, so I2C waits for WS.
static void watch_boot(int64_t t0, int64_t *ws_us, int64_t *i2c_us, int64_t *audio_us,
                       uint8_t ver[3]) {
    *ws_us = *i2c_us = *audio_us = -1;
    audio_at = 0;
    audio_watch = true;
    while (now_us() - t0 < 10000000) {
        if (ws_running()) {
            *ws_us = now_us() - t0;
            break;
        }
        vTaskDelay(1);
    }
    if (*ws_us < 0) return;
    while (now_us() - t0 < 12000000) {
        if (xmos_read(0xF0, 0x58, ver, 3) == ESP_OK) {
            *i2c_us = now_us() - t0;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    while (audio_watch && now_us() - t0 < 14000000) vTaskDelay(pdMS_TO_TICKS(5));
    if (!audio_watch) *audio_us = audio_at - t0;
    audio_watch = false;
}

static void cmd_xrst(void) {
    uint32_t txq = tx_qovf, rxq = rx_qovf;
    gpio_set_level(PIN_XRST, 1);
    esp_rom_delay_us(1000);
    gpio_set_level(PIN_XRST, 0);
    int64_t t0 = now_us();
    double ws_in_reset = edge_hz(pc_ws, 2000);
    int64_t ws, i2c, audio;
    uint8_t ver[3] = {0};
    watch_boot(t0, &ws, &i2c, &audio, ver);
    printf("XRST ws_in_reset_hz=%.0f ws_ms=%.1f i2c_ms=%.1f audio_ms=%.1f ver=%u.%u.%u "
           "tx_qovf+=%lu rx_qovf+=%lu\n",
           ws_in_reset, ws / 1000.0, i2c / 1000.0, audio / 1000.0, ver[0], ver[1], ver[2],
           (unsigned long)(tx_qovf - txq), (unsigned long)(rx_qovf - rxq));
}

// ---- WiFi, UDP ----

static EventGroupHandle_t wifi_ev;

static void wifi_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        printf("WIFI ip=" IPSTR "\n", IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(wifi_ev, 1);
    }
}

static void cmd_wifi(void) {
    static bool up;
    if (up) {
        printf("WIFI already\n");
        return;
    }
    up = true;
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    wifi_ev = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_handler, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_handler, NULL);
    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof wc.sta.ssid);
    strncpy((char *)wc.sta.password, WIFI_PASS, sizeof wc.sta.password);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);
    if (!(xEventGroupWaitBits(wifi_ev, 1, false, true, pdMS_TO_TICKS(15000)) & 1)) {
        printf("WIFI timeout\n");
    }
}

static void udp_rx_task(void *arg) {
    static int16_t pkt[UDP_FRAME];
    for (;;) {
        int n = recv(udp_sock, pkt, sizeof pkt, 0);
        if (n <= 0) continue;
        udp_in++;
        for (int i = 0; i < n / 2; i++) {
            if (spk16_count() == SPK16 - 1) {
                udp_over++;
                break;
            }
            spk16[spk16_head] = pkt[i];
            spk16_head = (spk16_head + 1) % SPK16;
        }
    }
}

static void cmd_udp(const char *host, int port) {
    if (udp_sock < 0) {
        udp_sock = socket(AF_INET, SOCK_DGRAM, 0);
        struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(UDP_PORT)};
        bind(udp_sock, (struct sockaddr *)&me, sizeof me);
        xTaskCreatePinnedToCore(udp_rx_task, "udp_rx", 4096, NULL, 18, NULL, 0);
    }
    udp_peer.sin_family = AF_INET;
    udp_peer.sin_port = htons(port);
    inet_pton(AF_INET, host, &udp_peer.sin_addr);
    spk16_primed = false;
    src = SRC_UDP;
    udp_on = true;
    printf("UDP on: listening %d, mic to %s:%d\n", UDP_PORT, host, port);
}

// ---- Console ----

static void cmd_top(int ms) {
    static TaskStatus_t a[40], b[40];
    configRUN_TIME_COUNTER_TYPE ta, tb;
    UBaseType_t na = uxTaskGetSystemState(a, 40, &ta);
    vTaskDelay(pdMS_TO_TICKS(ms));
    UBaseType_t nb = uxTaskGetSystemState(b, 40, &tb);
    double total = (double)(tb - ta);
    for (UBaseType_t i = 0; i < nb; i++) {
        for (UBaseType_t j = 0; j < na; j++) {
            if (a[j].xHandle != b[i].xHandle) continue;
            double pct = 100.0 * (double)(b[i].ulRunTimeCounter - a[j].ulRunTimeCounter) / total;
            printf("TOP %-12s core=%d prio=%u cpu=%.2f%% stack_free=%lu\n", b[i].pcTaskName,
                   b[i].xCoreID > 1 ? -1 : (int)b[i].xCoreID, (unsigned)b[i].uxCurrentPriority,
                   pct, (unsigned long)b[i].usStackHighWaterMark);
        }
    }
    printf("TOP end\n");
}

static void cmd_info(void) {
    printf("INFO reset=%d app_us=%lld boot_ws_already=%d boot_ws_ms=%.1f boot_i2c_ms=%.1f\n",
           esp_reset_reason(), (long long)t_app, boot_ws_already, boot_ws_us / 1000.0,
           boot_i2c_us / 1000.0);
    printf("INFO heap_int_free=%u heap_int_min=%u heap_int_largest=%u psram_free=%u\n",
           heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
           heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
           heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    printf("INFO tx_sent=%lu tx_writes=%lu tx_qovf=%lu tx_short=%lu rx_recv=%lu rx_frames=%lld "
           "rx_qovf=%lu rx_short=%lu loop=%d src=%d gain=%.4f lead=%u\n",
           (unsigned long)tx_sent, (unsigned long)tx_writes, (unsigned long)tx_qovf,
           (unsigned long)tx_short, (unsigned long)rx_recv, (long long)rx_frames,
           (unsigned long)rx_qovf, (unsigned long)rx_short, loop_on, src, sw_gain,
           (unsigned)cut.cfg.fetch_lead);
    printf("INFO udp_in=%lu udp_out=%lu udp_under=%lu udp_over=%lu spk16=%u\n",
           (unsigned long)udp_in, (unsigned long)udp_out, (unsigned long)udp_under,
           (unsigned long)udp_over, (unsigned)spk16_count());
}

static void cmd_level(int ms) {
    lvl.target = 0;
    lvl.sum[0] = lvl.sum[1] = 0;
    lvl.peak[0] = lvl.peak[1] = 0;
    lvl.n = 0;
    lvl.target = (size_t)ms * RATE / 1000;
    while (lvl.n < lvl.target) vTaskDelay(pdMS_TO_TICKS(10));
    for (int c = 0; c < 2; c++) {
        double rms = sqrt(lvl.sum[c] / lvl.n);
        printf("LEVEL ch%d rms_dbfs=%.1f peak_dbfs=%.1f\n", c, 20 * log10(rms + 1e-12),
               20 * log10(lvl.peak[c] / 2147483648.0 + 1e-12));
    }
}

static void write_all(const void *p, size_t n) {
    const uint8_t *b = p;
    while (n) {
        int w = usb_serial_jtag_write_bytes(b, n > 4096 ? 4096 : n, portMAX_DELAY);
        if (w <= 0) return;
        b += w;
        n -= (size_t)w;
    }
}

static void dump_bin(const char *name, const void *p, size_t n) {
    printf("DUMP %s %u\n", name, (unsigned)n);
    fflush(stdout);
    usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(100));
    write_all(p, n);
    printf("\nEND %s\n", name);
}

static void run_info(const char *tag) {
    int64_t silent = -1;
    tas_cut_silent_at(&cut, &silent);
    printf("%s now_us=%lld frames=%u frame0=%lld tx_start=%lld tx_base=%lu rx_base=%lu "
           "tx_sent=%lu rx_recv=%lu tx_qovf=%lu rx_qovf=%lu cut_us=%lld cut_ok=%d reach_us=%lld "
           "silent_us=%lld loop=%d descs=%d desc_frames=%d rate=%d\n",
           tag, (long long)now_us(), (unsigned)cap.n, (long long)cap.frame0,
           (long long)tx_start_frame, (unsigned long)tx_base, (unsigned long)rx_base,
           (unsigned long)tx_sent, (unsigned long)rx_recv, (unsigned long)tx_qovf,
           (unsigned long)rx_qovf, (long long)cut_us, cut_ok, (long long)cut_reach_us,
           (long long)silent, loop_on, DESCS, FRAMES, RATE);
}

static void cmd_run(int ms, bool play) {
    if (cap.on) {
        printf("ERR running\n");
        return;
    }
    cap.n = 0;
    cap.max = (size_t)ms * RATE / 1000;
    if (cap.max > CAP_MAX) {
        printf("ERR at most %d ms\n", CAP_MAX / (RATE / 1000));
        return;
    }
    stamp_on = false;
    tx_base = tx_sent;
    rx_base = rx_recv;
    stamp_on = true;
    cut_us = cut_reach_us = 0;
    tx_start_frame = -1;
    cap.n = 0;
    cap.on = true;
    if (play && load_len) {
        buf_pos = 0;
        src = SRC_BUF;
    }
    printf("RUN started\n");
}

static void cmd_cut(void) {
    cut_req = true;
    xTaskNotifyGive(tx_task_h);
    while (cut_req) vTaskDelay(1);
    printf("CUT ok=%d cut_us=%lld\n", cut_ok, (long long)cut_us);
}

static int cmp_i64(const void *a, const void *b) {
    int64_t x = *(const int64_t *)a, y = *(const int64_t *)b;
    return x < y ? -1 : x > y;
}

static void stats(const char *name, int64_t *v, int n) {
    if (!n) {
        printf("BENCH %s none\n", name);
        return;
    }
    qsort(v, n, sizeof *v, cmp_i64);
    printf("BENCH %s n=%d min=%lld p50=%lld p95=%lld p99=%lld max=%lld us\n", name, n,
           (long long)v[0], (long long)v[n / 2], (long long)v[n * 95 / 100], (long long)v[n * 99 / 100],
           (long long)v[n - 1]);
}

// `n` tone cuts: tas_cut's own estimate of the ramp's end, and, in loop mode, the line's last
// loud frame (>-60 dBFS), from the cut call.
static void cmd_cutbench(int n, float hz) {
    static int64_t model[500], line[500];
    int nm = 0, nl = 0, fails = 0;
    if (n > 500) n = 500;
    for (int i = 0; i < n; i++) {
        tone_hz = hz;
        tone_amp = 0.5f;
        src = SRC_TONE;
        vTaskDelay(pdMS_TO_TICKS(150 + esp_random() % 100));
        esp_rom_delay_us(esp_random() % 2500);
        loud_frame = -1;
        cut_req = true;
        xTaskNotifyGive(tx_task_h);
        while (cut_req) vTaskDelay(1);
        vTaskDelay(pdMS_TO_TICKS(12)); // the ramp is reached within a ring, and reported for a lap
        int64_t at;
        if (!cut_ok) fails++;
        if (cut_ok && tas_cut_silent_at(&cut, &at)) model[nm++] = at - cut_us;
        vTaskDelay(pdMS_TO_TICKS(48));
        if (loop_on && loud_frame >= 0) line[nl++] = loud_us - cut_us;
    }
    printf("BENCH fails=%d\n", fails);
    stats("model", model, nm);
    stats("line", line, nl);
}

// The lead scan, in loop mode: for each margin, `reps` markers at random delays after on_sent.
static void cmd_leadscan(int lo, int hi, int step, int reps) {
    if (!loop_on) {
        printf("ERR loop off\n");
        return;
    }
    src = SRC_SILENCE;
    vTaskDelay(pdMS_TO_TICKS(50));
    for (int m = lo; m <= hi; m += step) {
        int played = 0, tried = 0;
        for (int r = 0; r < reps; r++) {
            uint32_t seen = marks_seen;
            lead_m = m;
            lead_d_us = 100 + (int)(esp_random() % 1800);
            lead_req = true;
            xTaskNotifyGive(tx_task_h);
            while (lead_req) vTaskDelay(1);
            if (lead_pos < 0) continue;
            vTaskDelay(pdMS_TO_TICKS(30));
            tried++;
            if (marks_seen != seen) played++;
        }
        printf("LEAD m=%d played=%d/%d\n", m, played, tried);
    }
    printf("LEAD end\n");
}

static void cmd_load(size_t n) {
    src = SRC_SILENCE;
    vTaskDelay(pdMS_TO_TICKS(10));
    load_len = 0;
    if (n > LOAD_MAX * 2) {
        printf("ERR at most %d bytes\n", LOAD_MAX * 2);
        return;
    }
    printf("READY\n");
    fflush(stdout);
    size_t got = 0;
    uint32_t sum = 0;
    while (got < n) {
        int r = usb_serial_jtag_read_bytes((uint8_t *)load_buf + got, n - got, portMAX_DELAY);
        if (r > 0) got += (size_t)r;
    }
    for (size_t i = 0; i < n; i++) sum += ((uint8_t *)load_buf)[i];
    load_len = n / 2;
    printf("LOADED %u sum=%lu\n", (unsigned)n, (unsigned long)sum);
}

static void print_rate(void) {
    printf("RATE ws_hz=%.1f bclk_hz=%.0f mclk_hz=%.0f\n", edge_hz(pc_ws, 200000),
           edge_hz(pc_bclk, 5000), edge_hz(pc_mclk, 1000));
}

static size_t read_line(char *buf, size_t n) {
    size_t len = 0;
    for (;;) {
        uint8_t c;
        if (usb_serial_jtag_read_bytes(&c, 1, portMAX_DELAY) != 1) continue;
        if (c == '\r') continue;
        if (c == '\n') break;
        if (len + 1 < n) buf[len++] = (char)c;
    }
    buf[len] = 0;
    return len;
}

static const char *HELP =
    "info | rate | level [ms] | tone <hz> <dbfs> | stop | gain <db> | load <bytes> | play\n"
    "run <ms> [play] | wait | dump | cut | cutbench <n> [hz] | lead <frames>\n"
    "leadscan <lo> <hi> <step> <reps> | loop on|off | xrst | ver | vnr | spk 0|1\n"
    "aic <page> <reg> [val] | vol <db> | romlog on|off | reboot | wifi | udp <host> <port> | "
    "udp off | top [ms]\n";

static void console(void) {
    char line[128];
    for (;;) {
        read_line(line, sizeof line);
        char *argv[6] = {0};
        int argc = 0;
        for (char *t = strtok(line, " "); t && argc < 6; t = strtok(NULL, " ")) argv[argc++] = t;
        if (!argc) continue;
        const char *c = argv[0];
        if (!strcmp(c, "help")) {
            printf("%s", HELP);
        } else if (!strcmp(c, "info")) {
            cmd_info();
        } else if (!strcmp(c, "rate")) {
            print_rate();
        } else if (!strcmp(c, "level")) {
            cmd_level(argc > 1 ? atoi(argv[1]) : 500);
        } else if (!strcmp(c, "tone") && argc > 2) {
            tone_hz = (float)atof(argv[1]);
            tone_amp = powf(10, (float)atof(argv[2]) / 20);
            src = SRC_TONE;
            printf("OK\n");
        } else if (!strcmp(c, "stop")) {
            src = SRC_SILENCE;
            udp_on = false;
            printf("OK\n");
        } else if (!strcmp(c, "gain") && argc > 1) {
            sw_gain = powf(10, (float)atof(argv[1]) / 20);
            printf("OK gain=%.5f\n", sw_gain);
        } else if (!strcmp(c, "load") && argc > 1) {
            cmd_load((size_t)atol(argv[1]));
        } else if (!strcmp(c, "play")) {
            buf_pos = 0;
            src = load_len ? SRC_BUF : SRC_SILENCE;
            printf("OK\n");
        } else if (!strcmp(c, "run") && argc > 1) {
            cmd_run(atoi(argv[1]), argc > 2 && !strcmp(argv[2], "play"));
        } else if (!strcmp(c, "wait")) {
            while (cap.on) vTaskDelay(pdMS_TO_TICKS(5));
            run_info("RUN done");
        } else if (!strcmp(c, "dump")) {
            while (cap.on) vTaskDelay(pdMS_TO_TICKS(5));
            stamp_on = false;
            run_info("RUNINFO");
            uint32_t ntx = tx_sent - tx_base, nrx = rx_recv - rx_base;
            dump_bin("cap", cap.buf, cap.n * 8);
            dump_bin("rxt", rx_t, (nrx < MAXSEQ ? nrx : MAXSEQ) * 4);
            dump_bin("txt", tx_t, (ntx < MAXSEQ ? ntx : MAXSEQ) * 4);
        } else if (!strcmp(c, "cut")) {
            cmd_cut();
        } else if (!strcmp(c, "cutbench") && argc > 1) {
            cmd_cutbench(atoi(argv[1]), argc > 2 ? (float)atof(argv[2]) : 1000);
        } else if (!strcmp(c, "lead") && argc > 1) {
            cut.cfg.fetch_lead = (size_t)atoi(argv[1]);
            printf("OK\n");
        } else if (!strcmp(c, "leadscan") && argc > 4) {
            cmd_leadscan(atoi(argv[1]), atoi(argv[2]), atoi(argv[3]), atoi(argv[4]));
        } else if (!strcmp(c, "loop") && argc > 1) {
            set_loop(!strcmp(argv[1], "on"));
            printf("OK loop=%d\n", loop_on);
        } else if (!strcmp(c, "xrst")) {
            cmd_xrst();
        } else if (!strcmp(c, "ver")) {
            uint8_t v[3] = {0};
            esp_err_t err = xmos_read(0xF0, 0x58, v, 3);
            printf("VER err=%s %u.%u.%u\n", esp_err_to_name(err), v[0], v[1], v[2]);
        } else if (!strcmp(c, "vnr")) {
            uint8_t v[1] = {0}, m[1] = {0};
            esp_err_t e1 = xmos_read(0xF1, 0x00, v, 1), e2 = xmos_read(0xF1, 0x01, m, 1);
            printf("VNR %s vnr=%u mute %s=%u\n", esp_err_to_name(e1), v[0], esp_err_to_name(e2),
                   m[0]);
        } else if (!strcmp(c, "spk") && argc > 1) {
            uint8_t v = (uint8_t)atoi(argv[1]);
            printf("SPK %s\n", esp_err_to_name(xmos_write(0xF1, 0x10, &v, 1)));
        } else if (!strcmp(c, "aic") && argc > 2) {
            uint8_t page = (uint8_t)strtol(argv[1], NULL, 0), reg = (uint8_t)strtol(argv[2], NULL, 0);
            if (argc > 3) {
                uint8_t val = (uint8_t)strtol(argv[3], NULL, 0);
                printf("AIC w %s\n", esp_err_to_name(aic_write(page, reg, val)));
            } else {
                uint8_t val = 0;
                esp_err_t err = aic_read(page, reg, &val);
                printf("AIC p%u r%u = 0x%02x (%s)\n", page, reg, val, esp_err_to_name(err));
            }
        } else if (!strcmp(c, "vol") && argc > 1) {
            int v = (int)lrintf((float)atof(argv[1]) * 2);
            v = v < -127 ? -127 : v > 48 ? 48 : v;
            esp_err_t e1 = aic_write(0, 65, (uint8_t)(int8_t)v), e2 = aic_write(0, 66, (uint8_t)(int8_t)v);
            printf("VOL %.1f dB %s %s\n", v / 2.0, esp_err_to_name(e1), esp_err_to_name(e2));
        } else if (!strcmp(c, "romlog") && argc > 1) {
            if (!strcmp(argv[1], "off")) rtc_suppress_rom_log();
            else REG_CLR_BIT(RTC_CNTL_STORE4_REG, RTC_DISABLE_ROM_LOG);
            printf("OK store4=0x%08lx\n", (unsigned long)REG_READ(RTC_CNTL_STORE4_REG));
        } else if (!strcmp(c, "reboot")) {
            printf("OK\n");
            fflush(stdout);
            usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(100));
            esp_restart();
        } else if (!strcmp(c, "wifi")) {
            cmd_wifi();
        } else if (!strcmp(c, "udp") && argc > 1 && !strcmp(argv[1], "off")) {
            udp_on = false;
            src = SRC_SILENCE;
            printf("OK\n");
        } else if (!strcmp(c, "udp") && argc > 2) {
            cmd_udp(argv[1], atoi(argv[2]));
        } else if (!strcmp(c, "top")) {
            cmd_top(argc > 1 ? atoi(argv[1]) : 5000);
        } else {
            printf("ERR unknown: %s\n", c);
        }
        printf("#done\n");
        fflush(stdout);
    }
}

void app_main(void) {
    t_app = now_us();
    gpio_config_t rst = {.pin_bit_mask = 1ULL << PIN_XRST, .mode = GPIO_MODE_OUTPUT};
    gpio_set_level(PIN_XRST, 0); // high holds the XU316 in reset
    gpio_config(&rst);
    usb_serial_jtag_driver_config_t ucfg = {.tx_buffer_size = 16384, .rx_buffer_size = 16384};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&ucfg));
    usb_serial_jtag_vfs_use_driver();

    gpio_config_t mclk = {.pin_bit_mask = 1ULL << PIN_MCLK, .mode = GPIO_MODE_INPUT};
    gpio_config(&mclk);
    pc_ws = pcnt_on(PIN_WS);
    pc_bclk = pcnt_on(PIN_BCLK);
    pc_mclk = pcnt_on(PIN_MCLK);
    i2c_init();
    up_init();
    cap.buf = heap_caps_malloc(CAP_MAX * 8, MALLOC_CAP_SPIRAM);
    load_buf = heap_caps_malloc(LOAD_MAX * 2, MALLOC_CAP_SPIRAM);
    if (!cap.buf || !load_buf) abort();

    xTaskCreatePinnedToCore(tx_task, "tx", 4096, NULL, 22, &tx_task_h, 1);
    i2s_init();
    xTaskCreatePinnedToCore(rx_task, "rx", 4096, NULL, 21, NULL, 1);

    boot_ws_already = ws_running();
    int64_t audio;
    uint8_t ver[3] = {0};
    watch_boot(t_app, &boot_ws_us, &boot_i2c_us, &audio, ver);
    printf("BOOT reset=%d app_us=%lld ws_already=%d ws_ms=%.1f i2c_ms=%.1f audio_ms=%.1f "
           "ver=%u.%u.%u (from app start)\n",
           esp_reset_reason(), (long long)t_app, boot_ws_already, boot_ws_us / 1000.0,
           boot_i2c_us / 1000.0, audio / 1000.0, ver[0], ver[1], ver[2]);
    vTaskPrioritySet(NULL, 5);
    console();
}
