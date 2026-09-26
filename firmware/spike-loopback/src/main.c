// PROTOTYPE, throwaway: Atom Echo audio loopback spike for issue #7. Not the firmware.
//
// One TCP connection to the PC (hub/spike_loopback.py). Mic audio goes up as raw 16 kHz/16-bit
// mono PCM (optionally over UDP), speaker audio comes down the same way. Everything is stamped
// with esp_timer so the PC can compute latency, jitter, mic<->speaker switch time and heap.

#include <inttypes.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/i2s_pdm.h"
#include "soc/i2s_periph.h"
#include "esp_rom_gpio.h"
#include "lwip/sockets.h"
#include "secrets.h" // WIFI_SSID, WIFI_PASS, SPIKE_HOST, SPIKE_PORT; see secrets.h.example

#define TAG "spike"
#define RATE 16000
#define DESC_FRAMES 160 // 10 ms per DMA descriptor, both directions
#define RX_DESC_NUM 6   // 60 ms mic DMA ring
#define TX_DESC_NUM 10  // 100 ms speaker DMA ring, doubles as the prefill buffer
#define RX_DESC_BYTES (DESC_FRAMES * 2)
#define MAX_CHUNK_MS 40
#define PIN_BCLK 19
#define PIN_WS_PDMCLK 33 // one net: speaker LRCK and mic PDM clock, hence half duplex
#define PIN_DOUT 22
#define PIN_MIC_DIN 23
#define PIN_BTN 39

// Frame: u8 type, u8 reserved, u16 payload length (LE), payload. Same framing on TCP and UDP.
enum {
    F_PING = 0x01, F_MIC_START, F_MIC_STOP, F_SPK_START, F_SPK_DATA, F_SPK_END, F_SPK_STOP, F_STATS_REQ,
    F_RESET = 0x7F, // internal: connection lost
    F_HELLO = 0x81, F_PONG, F_MIC_DATA, F_MIC_STARTED, F_MIC_DONE, F_BTN,
    F_SPK_READY, F_SPK_PLAYING, F_SPK_ACK, F_SPK_DONE, F_STATS,
};

typedef struct __attribute__((packed)) { uint32_t seq; int64_t t_pc; } ping_t;
typedef struct __attribute__((packed)) { uint32_t seq; int64_t t_pc, t_dev; } pong_t;
typedef struct __attribute__((packed)) { uint8_t udp, slot_right, variant, chunk_ms, dsr16; uint16_t udp_port; } mic_start_t;
typedef struct __attribute__((packed)) { int64_t t_cmd, t_enabled; uint32_t teardown_us, setup_us; } mic_started_t;
typedef struct __attribute__((packed)) { uint32_t seq; int64_t t_cap_end; } mic_hdr_t;
typedef struct __attribute__((packed)) { uint32_t chunks, send_fail, dropped_descs; } mic_done_t;
typedef struct __attribute__((packed)) { uint8_t stereo, variant; uint16_t prefill_ms; } spk_start_t;
typedef struct __attribute__((packed)) { int64_t t_cmd; uint32_t teardown_us, setup_us; } spk_ready_t;
typedef struct __attribute__((packed)) { int64_t t_enable; uint32_t preloaded_bytes; } spk_playing_t;
typedef struct __attribute__((packed)) { uint32_t seq; int64_t t_pc; } spk_hdr_t;
typedef struct __attribute__((packed)) { uint32_t seq; int64_t t_dev_recv; } spk_ack_t;
typedef struct __attribute__((packed)) { uint8_t aborted; int64_t t_cmd, t_stop; uint32_t underrun_descs, bytes_played; } spk_done_t;
typedef struct __attribute__((packed)) { uint32_t free8, largest8, minfree8, free_internal; int32_t rssi; } stats_t;
typedef struct __attribute__((packed)) { uint32_t boot_free8, wifi_free8, conn_free8, conn_largest8, minfree8; char fw[16]; } hello_t;
typedef struct __attribute__((packed)) { uint8_t down; int64_t t_edge, t_sent; } btn_t;

typedef struct {
    uint8_t type;
    int64_t t_rx;
    union { mic_start_t mic; spk_start_t spk; };
} cmd_t;

static int s_sock = -1, s_udp = -1;
static struct sockaddr_in s_peer;
static SemaphoreHandle_t s_send_mx;
static QueueHandle_t s_cmdq;
static StreamBufferHandle_t s_spk_sb;
static TaskHandle_t s_btn_task;
static EventGroupHandle_t s_wifi_ev;
static uint32_t s_heap_boot, s_heap_wifi;

static uint32_t free8(void) { return heap_caps_get_free_size(MALLOC_CAP_8BIT); }
static uint32_t largest8(void) { return heap_caps_get_largest_free_block(MALLOC_CAP_8BIT); }
static uint32_t minfree8(void) { return heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT); }

// ---------------------------------------------------------------- sending

static bool send_all(int s, const uint8_t *p, size_t n) {
    while (n) {
        int w = send(s, p, n, 0);
        if (w <= 0) return false;
        p += w;
        n -= w;
    }
    return true;
}

static uint8_t s_txbuf[4 + sizeof(mic_hdr_t) + MAX_CHUNK_MS * RATE / 1000 * 2];

// udp_port 0 = TCP connection, otherwise UDP to the TCP peer's address on that port.
static bool send_frame(uint8_t type, const void *a, size_t na, const void *b, size_t nb, uint16_t udp_port) {
    size_t len = na + nb;
    xSemaphoreTake(s_send_mx, portMAX_DELAY);
    s_txbuf[0] = type;
    s_txbuf[1] = 0;
    s_txbuf[2] = len & 0xff;
    s_txbuf[3] = len >> 8;
    if (na) memcpy(s_txbuf + 4, a, na);
    if (nb) memcpy(s_txbuf + 4 + na, b, nb);
    bool ok;
    if (udp_port) {
        struct sockaddr_in dst = s_peer;
        dst.sin_port = htons(udp_port);
        ok = sendto(s_udp, s_txbuf, 4 + len, 0, (struct sockaddr *)&dst, sizeof dst) == (int)(4 + len);
    } else {
        ok = s_sock >= 0 && send_all(s_sock, s_txbuf, 4 + len);
    }
    xSemaphoreGive(s_send_mx);
    return ok;
}
#define SEND(type, st) send_frame(type, &(st), sizeof(st), NULL, 0, 0)

// ---------------------------------------------------------------- I2S
// Mic = PDM RX on I2S0 (PDM->PCM exists only there), speaker = std TX on I2S1. Both need G33.
// variant 0: one channel alive at a time, delete + create on every switch (what ESPHome does).
// variant 1: both stay allocated; a switch disables one, re-routes G33 in the GPIO matrix, enables the other.

static i2s_chan_handle_t s_rx, s_tx;
static bool s_rx_on, s_tx_on;
static int s_rx_slot = -1, s_rx_dsr16 = -1, s_tx_stereo = -1, s_variant = -1;

static volatile uint32_t s_rx_descs, s_rx_dropped;
static int64_t s_rx_desc_t[64]; // completion time of each mic DMA descriptor
static volatile uint32_t s_tx_sent, s_tx_written, s_tx_slip, s_tx_underruns;
static volatile bool s_tx_counting;

static bool IRAM_ATTR on_recv(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    s_rx_desc_t[s_rx_descs & 63] = esp_timer_get_time();
    s_rx_descs++;
    return false;
}

static bool IRAM_ATTR on_recv_ovf(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    s_rx_dropped++;
    return false;
}

// Underrun = the DMA finished a descriptor the writer hadn't filled yet (it played auto-cleared zeros).
static bool IRAM_ATTR on_sent(i2s_chan_handle_t h, i2s_event_data_t *e, void *ctx) {
    uint32_t sent = ++s_tx_sent;
    if (s_tx_counting && sent > s_tx_written + s_tx_slip) {
        s_tx_underruns++;
        s_tx_slip = sent - s_tx_written;
    }
    return false;
}

static void rx_create(int slot_right, int dsr16) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.dma_desc_num = RX_DESC_NUM;
    cc.dma_frame_num = DESC_FRAMES;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, NULL, &s_rx));
    i2s_pdm_rx_config_t pc = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .clk = PIN_WS_PDMCLK, .din = PIN_MIC_DIN },
    };
    pc.clk_cfg.dn_sample_mode = dsr16 ? I2S_PDM_DSR_16S : I2S_PDM_DSR_8S; // PDM clk 2.048 vs 1.024 MHz
    pc.slot_cfg.slot_mask = slot_right ? I2S_PDM_SLOT_RIGHT : I2S_PDM_SLOT_LEFT;
    ESP_ERROR_CHECK(i2s_channel_init_pdm_rx_mode(s_rx, &pc));
    i2s_event_callbacks_t cb = { .on_recv = on_recv, .on_recv_q_ovf = on_recv_ovf };
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(s_rx, &cb, NULL));
    s_rx_slot = slot_right;
    s_rx_dsr16 = dsr16;
}

static void tx_create(int stereo) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    cc.dma_desc_num = TX_DESC_NUM;
    cc.dma_frame_num = DESC_FRAMES;
    cc.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &s_tx, NULL));
    i2s_slot_mode_t mode = stereo ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO;
    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, mode),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = PIN_BCLK, .ws = PIN_WS_PDMCLK, .dout = PIN_DOUT, .din = I2S_GPIO_UNUSED },
    };
    sc.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &sc));
    i2s_event_callbacks_t cb = { .on_sent = on_sent };
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(s_tx, &cb, NULL));
    s_tx_stereo = stereo;
}

static void rx_off(void) {
    if (s_rx_on) i2s_channel_disable(s_rx);
    s_rx_on = false;
}

static void tx_off(void) {
    s_tx_counting = false;
    if (s_tx_on) i2s_channel_disable(s_tx);
    s_tx_on = false;
}

static void rx_free(void) {
    rx_off();
    if (s_rx) i2s_del_channel(s_rx);
    s_rx = NULL;
}

static void tx_free(void) {
    tx_off();
    if (s_tx) i2s_del_channel(s_tx);
    s_tx = NULL;
}

static void route_ws(bool to_mic) {
    esp_rom_gpio_connect_out_signal(PIN_WS_PDMCLK,
                                    to_mic ? i2s_periph_signal[0].m_rx_ws_sig : i2s_periph_signal[1].m_tx_ws_sig,
                                    false, false);
}

static void to_mic(const mic_start_t *m, uint32_t *teardown_us, uint32_t *setup_us) {
    int64_t t0 = esp_timer_get_time();
    if (m->variant != s_variant || (s_rx && (s_rx_slot != m->slot_right || s_rx_dsr16 != m->dsr16))) {
        tx_free();
        rx_free();
        s_variant = m->variant;
    }
    if (m->variant == 0) tx_free();
    else tx_off();
    rx_off();
    int64_t t1 = esp_timer_get_time();
    if (!s_rx) rx_create(m->slot_right, m->dsr16);
    if (m->variant == 1) {
        if (!s_tx) tx_create(s_tx_stereo < 0 ? 1 : s_tx_stereo);
        route_ws(true);
    }
    s_rx_descs = 0;
    s_rx_dropped = 0;
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx));
    s_rx_on = true;
    *teardown_us = t1 - t0;
    *setup_us = esp_timer_get_time() - t1;
}

static void to_spk(const spk_start_t *sp, uint32_t *teardown_us, uint32_t *setup_us) {
    int64_t t0 = esp_timer_get_time();
    if (sp->variant != s_variant) {
        tx_free();
        rx_free();
        s_variant = sp->variant;
    }
    if (s_tx && s_tx_stereo != sp->stereo) tx_free();
    tx_off();
    if (sp->variant == 0) rx_free();
    else rx_off();
    int64_t t1 = esp_timer_get_time();
    if (!s_tx) tx_create(sp->stereo);
    if (sp->variant == 1) {
        if (!s_rx) rx_create(s_rx_slot < 0 ? 1 : s_rx_slot, s_rx_dsr16 < 0 ? 1 : s_rx_dsr16);
        route_ws(false);
    }
    s_tx_sent = s_tx_written = s_tx_slip = s_tx_underruns = 0;
    *teardown_us = t1 - t0;
    *setup_us = esp_timer_get_time() - t1;
}

static void spk_start_playing(uint32_t preloaded) {
    s_tx_counting = true;
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));
    s_tx_on = true;
    spk_playing_t p = { .t_enable = esp_timer_get_time(), .preloaded_bytes = preloaded };
    SEND(F_SPK_PLAYING, p);
}

static void spk_done(uint8_t aborted, int64_t t_cmd, uint32_t bytes) {
    uint32_t underruns = s_tx_underruns;
    tx_off(); // with abort, this is the moment the speaker goes silent
    spk_done_t d = { .aborted = aborted, .t_cmd = t_cmd, .t_stop = esp_timer_get_time(),
                     .underrun_descs = underruns, .bytes_played = bytes };
    SEND(F_SPK_DONE, d);
}

// Owns I2S. Commands arrive from the connection task via s_cmdq, speaker PCM via s_spk_sb.
static void audio_task(void *arg) {
    static int16_t mic[MAX_CHUNK_MS * RATE / 1000];
    static int16_t mono[DESC_FRAMES], st[DESC_FRAMES * 2];
    enum { IDLE, MIC, SPK } mode = IDLE;
    cmd_t c;
    mic_start_t m = { 0 };
    uint32_t mic_seq = 0, mic_fail = 0, mic_bytes = 0, chunk_bytes = 0;
    spk_start_t sp = { 0 };
    bool playing = false, ending = false;
    int64_t end_t_cmd = 0;
    uint32_t spk_bytes = 0, preloaded = 0, prefill_bytes = 0, tx_desc_bytes = 0;

    for (;;) {
        while (xQueueReceive(s_cmdq, &c, mode == IDLE ? portMAX_DELAY : 0) == pdTRUE) {
            bool starts = c.type == F_MIC_START || c.type == F_SPK_START;
            if (mode == MIC && (starts || c.type == F_MIC_STOP || c.type == F_RESET)) {
                rx_off();
                mic_done_t d = { mic_seq, mic_fail, s_rx_dropped };
                SEND(F_MIC_DONE, d);
                mode = IDLE;
            }
            if (mode == SPK && (starts || c.type == F_SPK_STOP || c.type == F_RESET)) {
                spk_done(1, c.t_rx, spk_bytes);
                if (c.type != F_SPK_START) { // drop whatever of the aborted clip is still queued
                    uint8_t junk[64];
                    while (xStreamBufferReceive(s_spk_sb, junk, sizeof junk, 0)) {}
                }
                mode = IDLE;
            }
            if (c.type == F_MIC_START) {
                m = c.mic;
                if (m.chunk_ms < 10 || m.chunk_ms > MAX_CHUNK_MS) m.chunk_ms = 20;
                m.chunk_ms -= m.chunk_ms % 10;
                chunk_bytes = m.chunk_ms * RATE / 1000 * 2;
                uint32_t td, su;
                to_mic(&m, &td, &su);
                mic_started_t r = { .t_cmd = c.t_rx, .t_enabled = esp_timer_get_time(), .teardown_us = td, .setup_us = su };
                SEND(F_MIC_STARTED, r);
                mic_seq = mic_fail = mic_bytes = 0;
                mode = MIC;
            } else if (c.type == F_SPK_START) {
                sp = c.spk;
                uint32_t td, su;
                to_spk(&sp, &td, &su);
                spk_ready_t r = { .t_cmd = c.t_rx, .teardown_us = td, .setup_us = su };
                SEND(F_SPK_READY, r);
                tx_desc_bytes = DESC_FRAMES * (sp.stereo ? 4 : 2);
                prefill_bytes = sp.prefill_ms * RATE / 1000 * (sp.stereo ? 4 : 2);
                if (prefill_bytes > (TX_DESC_NUM - 1) * tx_desc_bytes) prefill_bytes = (TX_DESC_NUM - 1) * tx_desc_bytes;
                playing = ending = false;
                spk_bytes = preloaded = 0;
                mode = SPK;
            } else if (c.type == F_SPK_END && mode == SPK) {
                ending = true;
                end_t_cmd = c.t_rx;
            }
        }

        if (mode == MIC) {
            size_t got = 0;
            if (i2s_channel_read(s_rx, mic, chunk_bytes, &got, 200) != ESP_OK || got == 0) continue;
            mic_bytes += got;
            uint32_t idx = mic_bytes / RX_DESC_BYTES + s_rx_dropped;
            mic_hdr_t h = { .seq = mic_seq++, .t_cap_end = s_rx_desc_t[(idx - 1) & 63] };
            if (!send_frame(F_MIC_DATA, &h, sizeof h, mic, got, m.udp ? m.udp_port : 0)) mic_fail++;
        } else if (mode == SPK) {
            size_t n = xStreamBufferReceive(s_spk_sb, mono, sizeof mono, ending ? 0 : pdMS_TO_TICKS(5));
            if (n) {
                const uint8_t *out = (const uint8_t *)mono;
                size_t out_n = n, done = 0;
                if (sp.stereo) { // mono on the ESP32 comes out word-swapped and tinny; send L=R instead
                    for (size_t i = 0; i < n / 2; i++) st[2 * i] = st[2 * i + 1] = mono[i];
                    out = (const uint8_t *)st;
                    out_n = n * 2;
                }
                if (!playing) {
                    i2s_channel_preload_data(s_tx, out, out_n, &done);
                    preloaded += done;
                    s_tx_written = (spk_bytes + done) / tx_desc_bytes;
                    if (done < out_n || preloaded >= prefill_bytes) {
                        spk_start_playing(preloaded);
                        playing = true;
                    }
                }
                if (done < out_n) {
                    size_t w = 0;
                    i2s_channel_write(s_tx, out + done, out_n - done, &w, 200);
                    done += w;
                }
                spk_bytes += done;
                s_tx_written = spk_bytes / tx_desc_bytes;
            } else if (ending) {
                s_tx_counting = false; // the tail drains into silence, which isn't an underrun
                if (!playing) {
                    spk_start_playing(preloaded);
                    playing = true;
                }
                if (s_tx_sent >= (spk_bytes + tx_desc_bytes - 1) / tx_desc_bytes + s_tx_slip) {
                    spk_done(0, end_t_cmd, spk_bytes);
                    mode = IDLE;
                } else {
                    vTaskDelay(pdMS_TO_TICKS(2));
                }
            }
        }
    }
}

// ---------------------------------------------------------------- button (G39, active low)

static volatile int64_t s_btn_t;
static volatile bool s_btn_pending;

static void IRAM_ATTR btn_isr(void *arg) {
    if (!s_btn_pending) {
        s_btn_t = esp_timer_get_time();
        s_btn_pending = true;
    }
    BaseType_t hp = pdFALSE;
    vTaskNotifyGiveFromISR(s_btn_task, &hp);
    if (hp) portYIELD_FROM_ISR();
}

static void btn_task(void *arg) {
    int state = 0;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        vTaskDelay(pdMS_TO_TICKS(5)); // debounce; also rejects the ESP32 GPIO39 glitch pulses
        int64_t t = s_btn_t;
        s_btn_pending = false;
        int down = !gpio_get_level(PIN_BTN);
        if (down == state) continue;
        state = down;
        btn_t b = { .down = down, .t_edge = t, .t_sent = esp_timer_get_time() };
        ESP_LOGI(TAG, "button %s", down ? "down" : "up");
        SEND(F_BTN, b);
    }
}

// ---------------------------------------------------------------- connection

static bool recv_all(int s, void *buf, size_t n) {
    uint8_t *p = buf;
    while (n) {
        int r = recv(s, p, n, 0);
        if (r <= 0) return false;
        p += r;
        n -= r;
    }
    return true;
}

static void rx_loop(int s) {
    static uint8_t buf[2048];
    for (;;) {
        uint8_t hdr[4];
        if (!recv_all(s, hdr, 4)) return;
        uint16_t len = hdr[2] | hdr[3] << 8;
        if (len > sizeof buf || !recv_all(s, buf, len)) return;
        int64_t t = esp_timer_get_time();
        cmd_t c = { .type = hdr[0], .t_rx = t };
        switch (hdr[0]) {
        case F_PING: {
            ping_t p;
            memcpy(&p, buf, sizeof p);
            pong_t r = { .seq = p.seq, .t_pc = p.t_pc, .t_dev = t };
            SEND(F_PONG, r);
            break;
        }
        case F_SPK_DATA: {
            spk_hdr_t h;
            memcpy(&h, buf, sizeof h);
            spk_ack_t a = { .seq = h.seq, .t_dev_recv = t };
            SEND(F_SPK_ACK, a);
            xStreamBufferSend(s_spk_sb, buf + sizeof h, len - sizeof h, pdMS_TO_TICKS(500));
            break;
        }
        case F_STATS_REQ: {
            stats_t st = { free8(), largest8(), minfree8(), heap_caps_get_free_size(MALLOC_CAP_INTERNAL), 0 };
            wifi_ap_record_t ap;
            if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) st.rssi = ap.rssi;
            SEND(F_STATS, st);
            break;
        }
        case F_MIC_START:
            memcpy(&c.mic, buf, sizeof c.mic);
            xQueueSend(s_cmdq, &c, portMAX_DELAY);
            break;
        case F_SPK_START:
            memcpy(&c.spk, buf, sizeof c.spk);
            xQueueSend(s_cmdq, &c, portMAX_DELAY);
            break;
        case F_MIC_STOP:
        case F_SPK_END:
        case F_SPK_STOP:
            xQueueSend(s_cmdq, &c, portMAX_DELAY);
            break;
        }
    }
}

static void wifi_ev(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED)) {
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(s_wifi_ev, 1);
    }
}

static void wifi_up(void) {
    s_wifi_ev = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_ev, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_ev, NULL));
    wifi_config_t wc = { 0 };
    strlcpy((char *)wc.sta.ssid, WIFI_SSID, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, WIFI_PASS, sizeof wc.sta.password);
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE)); // default modem sleep batches RX to the DTIM period
    xEventGroupWaitBits(s_wifi_ev, 1, pdFALSE, pdTRUE, portMAX_DELAY);
}

void app_main(void) {
    // M5Unified drives G0 high on Atom boards: otherwise the CH552 holds it near 4 V, hurting WiFi sensitivity.
    gpio_set_direction(GPIO_NUM_0, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_0, 1);

    s_heap_boot = free8();
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    wifi_up();
    s_heap_wifi = free8();
    ESP_LOGI(TAG, "heap free8: boot %" PRIu32 ", after wifi %" PRIu32, s_heap_boot, s_heap_wifi);

    s_send_mx = xSemaphoreCreateMutex();
    s_cmdq = xQueueCreate(16, sizeof(cmd_t));
    s_spk_sb = xStreamBufferCreate(8192, 1);
    s_udp = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    xTaskCreatePinnedToCore(audio_task, "audio", 4096, NULL, 10, NULL, 1);
    xTaskCreate(btn_task, "btn", 3072, NULL, 12, &s_btn_task);

    gpio_config_t io = { .pin_bit_mask = 1ULL << PIN_BTN, .mode = GPIO_MODE_INPUT, .intr_type = GPIO_INTR_ANYEDGE };
    ESP_ERROR_CHECK(gpio_config(&io));
    ESP_ERROR_CHECK(gpio_install_isr_service(0));
    ESP_ERROR_CHECK(gpio_isr_handler_add(PIN_BTN, btn_isr, NULL));

    for (;;) {
        int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(SPIKE_PORT) };
        inet_pton(AF_INET, SPIKE_HOST, &a.sin_addr);
        if (connect(s, (struct sockaddr *)&a, sizeof a) != 0) {
            close(s);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        int one = 1;
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        s_peer = a;
        hello_t h = { .boot_free8 = s_heap_boot, .wifi_free8 = s_heap_wifi, .conn_free8 = free8(),
                      .conn_largest8 = largest8(), .minfree8 = minfree8(), .fw = "spike-loopback" };
        s_sock = s;
        SEND(F_HELLO, h);
        ESP_LOGI(TAG, "connected to %s:%d", SPIKE_HOST, SPIKE_PORT);
        rx_loop(s);
        ESP_LOGW(TAG, "disconnected");
        xSemaphoreTake(s_send_mx, portMAX_DELAY);
        s_sock = -1;
        xSemaphoreGive(s_send_mx);
        close(s);
        cmd_t c = { .type = F_RESET, .t_rx = esp_timer_get_time() };
        xQueueSend(s_cmdq, &c, portMAX_DELAY);
    }
}
