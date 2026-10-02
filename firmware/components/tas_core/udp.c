// The UDP socket: stream frames to and from the hub (docs/protocol/device.md, "UDP datagrams").
// One socket for the device's life, bound before the first hello, so every hello sends the same
// udp_port. The udp_rx task hands the hub's speaker frames to the Kinds; the udp_tx task sends the
// mic frames they queue, and the event mirrors, each twice, first. Datagrams flow only while a
// Connection is welcomed.

#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "core.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "tas_conn.h"
#include "tas_kinds.h"

#define TAG "udp"
#define UDP_CORE 0
#define UDP_PRIORITY 15
#define UDP_STACK 3072
#define WAIT_MS 1000 // the longest a wait goes between watchdog feeds
#define MIRROR_COPIES 2
#define MIRROR_GAP_US 5000
#define MIRRORS_MAX 8 // waiting at once

static int sock = -1;
static uint16_t port;
static TaskHandle_t tx_handle;

// The welcomed Connection's hub: where datagrams go, and which are ours.
static portMUX_TYPE peer_lock = portMUX_INITIALIZER_UNLOCKED;
static bool welcomed;
static struct sockaddr_in hub;
static uint32_t conn_id;

// Sending takes the frames in order, whoever sends: the udp_tx task, or a mic's stop.
static SemaphoreHandle_t send_lock;
static StaticSemaphore_t send_lock_buf;

// An event's mirror, to send.
typedef struct {
    uint32_t conn_id, seq;
    tas_kind_event_t event;
    int64_t due_us; // the next copy's time
    uint8_t sent;   // copies sent
} mirror_t;

// The mirrors waiting, oldest first. A spinlock: held for a few copies of at most MIRRORS_MAX.
static portMUX_TYPE mirrors_lock = portMUX_INITIALIZER_UNLOCKED;
static mirror_t mirrors[MIRRORS_MAX];
static size_t mirror_count;

// What came of the datagrams, since boot.
static atomic_uint rx_frames, rx_late, rx_unknown, rx_bad, rx_foreign, tx_frames, tx_mirrors,
    tx_failed;

// The hub and conn_id. False if no Connection is welcomed.
static bool get_peer(struct sockaddr_in *to, uint32_t *id) {
    taskENTER_CRITICAL(&peer_lock);
    bool up = welcomed;
    *to = hub;
    *id = conn_id;
    taskEXIT_CRITICAL(&peer_lock);
    return up;
}

void udp_welcomed(struct in_addr hub_ip, uint16_t hub_port, uint32_t id) {
    taskENTER_CRITICAL(&peer_lock);
    hub = (struct sockaddr_in){
        .sin_family = AF_INET, .sin_addr = hub_ip, .sin_port = htons(hub_port)};
    conn_id = id;
    welcomed = true;
    taskEXIT_CRITICAL(&peer_lock);
}

void udp_closed(void) {
    taskENTER_CRITICAL(&peer_lock);
    welcomed = false;
    taskEXIT_CRITICAL(&peer_lock);
    taskENTER_CRITICAL(&mirrors_lock);
    mirror_count = 0;
    taskEXIT_CRITICAL(&mirrors_lock);
}

uint16_t udp_port(void) {
    return port;
}

// One datagram from `from`.
static void receive(const uint8_t *data, size_t len, const struct sockaddr_in *from) {
    struct sockaddr_in to;
    uint32_t id;
    if (!get_peer(&to, &id)) return; // none flow before welcome
    tas_dgram_t dgram;
    tas_wire_err_t err = tas_dgram_decode(data, len, &dgram);
    if (from->sin_addr.s_addr != to.sin_addr.s_addr || (err == TAS_WIRE_OK && dgram.conn_id != id)) {
        atomic_fetch_add(&rx_foreign, 1);
        return;
    }
    if (err != TAS_WIRE_OK) {
        atomic_fetch_add(&rx_bad, 1);
        return;
    }
    if (dgram.kind != TAS_DGRAM_STREAM) return; // mirrors go the other way; other kinds are new
    switch (tas_kinds_frame_in(&dgram, &err)) {
    case TAS_FRAME_TAKEN: atomic_fetch_add(&rx_frames, 1); break;
    case TAS_FRAME_LATE: atomic_fetch_add(&rx_late, 1); break;
    case TAS_FRAME_BAD: atomic_fetch_add(&rx_bad, 1); break;
    case TAS_FRAME_CLOSED: break;
    case TAS_FRAME_UNKNOWN: atomic_fetch_add(&rx_unknown, 1); break;
    }
}

static void rx_task(void *arg) {
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    // The payload, at offset 20, stays aligned for the PCM in it. One byte more than a datagram
    // may hold, to tell one that's too large.
    static _Alignas(4) uint8_t buf[TAS_UDP_HEADER + TAS_MAX_UDP_PAYLOAD + 1];
    for (;;) {
        core_breathe();
        struct sockaddr_in from;
        socklen_t from_len = sizeof from;
        ssize_t n = recvfrom(sock, buf, sizeof buf, 0, (struct sockaddr *)&from, &from_len);
        if (n >= 0) receive(buf, (size_t)n, &from);
    }
}

void udp_send_frames(void) {
    // Under send_lock.
    static tas_mic_frame_t frame;
    static uint8_t buf[TAS_UDP_HEADER + TAS_MAX_UDP_PAYLOAD];
    xSemaphoreTake(send_lock, portMAX_DELAY);
    while (tas_kinds_frame_out(&frame)) {
        struct sockaddr_in to;
        uint32_t id;
        if (!get_peer(&to, &id)) continue; // nowhere to go
        tas_dgram_t dgram = {.conn_id = id,
                             .kind = TAS_DGRAM_STREAM,
                             .stream = frame.stream,
                             .seq = frame.seq,
                             .ts = frame.ts,
                             .payload = (const uint8_t *)frame.pcm,
                             .len = frame.samples * sizeof *frame.pcm};
        int n = tas_dgram_encode(&dgram, buf, sizeof buf);
        if (n > 0 && sendto(sock, buf, (size_t)n, 0, (struct sockaddr *)&to, sizeof to) == n) {
            atomic_fetch_add(&tx_frames, 1);
        } else {
            atomic_fetch_add(&tx_failed, 1);
        }
    }
    xSemaphoreGive(send_lock);
}

void udp_frames_ready(void) {
    xTaskNotifyGive(tx_handle);
}

void udp_mirror(uint32_t seq, const tas_kind_event_t *event) {
    struct sockaddr_in to;
    uint32_t id;
    if (!get_peer(&to, &id)) return;
    taskENTER_CRITICAL(&mirrors_lock);
    bool room = mirror_count < MIRRORS_MAX;
    if (room) {
        mirrors[mirror_count++] = (mirror_t){
            .conn_id = id, .seq = seq, .event = *event, .due_us = esp_timer_get_time()};
    }
    taskEXIT_CRITICAL(&mirrors_lock);
    if (room) xTaskNotifyGive(tx_handle);
    else atomic_fetch_add(&tx_failed, 1);
}

// Sends one copy of `m`, if its Connection is still the one welcomed.
static void send_mirror(const mirror_t *m) {
    static uint8_t buf[TAS_UDP_HEADER + TAS_MAX_UDP_PAYLOAD]; // only udp_tx's
    struct sockaddr_in to;
    uint32_t id;
    if (!get_peer(&to, &id) || id != m->conn_id) return;
    const tas_kind_event_t *e = &m->event;
    int n = tas_conn_mirror(id, m->seq, e->cap, e->name, e->ts, e->data, e->len, buf, sizeof buf);
    if (n > 0 && sendto(sock, buf, (size_t)n, 0, (struct sockaddr *)&to, sizeof to) == n) {
        atomic_fetch_add(&tx_mirrors, 1);
    } else {
        atomic_fetch_add(&tx_failed, 1);
    }
}

// Sends the mirror copies due now. When the next one is due, or INT64_MAX for none.
static int64_t send_mirrors(void) {
    static mirror_t due[MIRRORS_MAX]; // only udp_tx's
    size_t n = 0;
    int64_t now = esp_timer_get_time(), next = INT64_MAX;
    taskENTER_CRITICAL(&mirrors_lock);
    for (size_t i = 0; i < mirror_count;) {
        mirror_t *m = &mirrors[i];
        if (m->due_us <= now) {
            due[n++] = *m;
            if (++m->sent == MIRROR_COPIES) {
                memmove(m, m + 1, (--mirror_count - i) * sizeof *m);
                continue;
            }
            m->due_us = now + MIRROR_GAP_US;
        }
        if (m->due_us < next) next = m->due_us;
        i++;
    }
    taskEXIT_CRITICAL(&mirrors_lock);
    for (size_t i = 0; i < n; i++) send_mirror(&due[i]);
    return next;
}

static void tx_task(void *arg) {
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    TickType_t wait = pdMS_TO_TICKS(WAIT_MS);
    for (;;) {
        core_breathe();
        ulTaskNotifyTake(pdTRUE, wait);
        int64_t next = send_mirrors();
        udp_send_frames();
        wait = pdMS_TO_TICKS(WAIT_MS);
        if (next != INT64_MAX) {
            int64_t ms = (next - esp_timer_get_time() + 999) / 1000;
            if (ms < WAIT_MS) wait = ms > 0 ? pdMS_TO_TICKS(ms) : 0;
        }
    }
}

void udp_start(void) {
    send_lock = xSemaphoreCreateMutexStatic(&send_lock_buf);
    sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_ANY)};
    socklen_t len = sizeof addr;
    struct timeval timeout = {.tv_sec = WAIT_MS / 1000};
    if (sock < 0 || bind(sock, (struct sockaddr *)&addr, sizeof addr) < 0 ||
        getsockname(sock, (struct sockaddr *)&addr, &len) < 0 ||
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout) < 0) {
        ESP_LOGE(TAG, "socket: %d", errno);
        abort();
    }
    port = ntohs(addr.sin_port);
    xTaskCreatePinnedToCore(rx_task, "udp_rx", UDP_STACK, NULL, UDP_PRIORITY, NULL, UDP_CORE);
    xTaskCreatePinnedToCore(tx_task, "udp_tx", UDP_STACK, NULL, UDP_PRIORITY, &tx_handle,
                            UDP_CORE);
    ESP_LOGI(TAG, "port %u", port);
}

void udp_describe(char *buf, size_t size) {
    snprintf(buf, size,
             "port %u; in %u frames, %u late, %u unknown, %u bad, %u foreign; out %u frames, "
             "%u mirrors, %u failed",
             port, atomic_load(&rx_frames), atomic_load(&rx_late), atomic_load(&rx_unknown),
             atomic_load(&rx_bad), atomic_load(&rx_foreign), atomic_load(&tx_frames),
             atomic_load(&tx_mirrors), atomic_load(&tx_failed));
}
