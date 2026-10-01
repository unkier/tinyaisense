// The ctrl task: the only TCP writer. It finds the hub, runs one Connection at a time through
// tas_conn, and reconnects with backoff. It waits in select() on the socket plus an eventfd that
// other tasks kick.

#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <string.h>
#include <unistd.h>

#include "core.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_vfs_eventfd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tas_backoff.h"
#include "tas_conn.h"

#define TAG "ctrl"
#define CTRL_CORE 0
#define CTRL_PRIORITY 10
#define CTRL_STACK 6144
#define CONNECT_TIMEOUT_US 3000000
#define BREATHE_US 1000000 // the longest a wait goes between watchdog feeds

static const tas_manifest_t *manifest;
static tas_conn_t conn;         // 12 KB, so kept off the stack
static atomic_int kick_fd = -1; // -1 until ctrl_start: kicks before then are moot
static atomic_uint kicks;
static atomic_int state = CTRL_IDLE;
static atomic_uint lost; // welcomed Connections that ended

static portMUX_TYPE describe_mux = portMUX_INITIALIZER_UNLOCKED;
static char peer_name[TAS_CONN_MAX_NAME + 1], peer_addr[HUB_ADDR_MAX];

static uint64_t now_us(void) {
    return (uint64_t)esp_timer_get_time();
}

// select() for up to `wait_us`, but no longer than the watchdog allows; then feeds it.
static int breathe_select(int nfds, fd_set *rd, fd_set *wr, uint64_t wait_us) {
    if (wait_us > BREATHE_US) wait_us = BREATHE_US;
    struct timeval tv = {.tv_sec = wait_us / 1000000, .tv_usec = wait_us % 1000000};
    int n = select(nfds, rd, wr, NULL, &tv);
    core_breathe();
    return n;
}

void ctrl_kick(uint32_t why) {
    int fd = atomic_load(&kick_fd);
    if (fd < 0) return;
    atomic_fetch_or(&kicks, why);
    uint64_t one = 1;
    if (write(fd, &one, sizeof one) < 0) ESP_LOGE(TAG, "kick: %d", errno);
}

// The kicks since the last take. Call it once select() says the eventfd is readable.
static uint32_t take_kicks(int fd) {
    uint64_t count;
    if (read(fd, &count, sizeof count) < 0) ESP_LOGE(TAG, "kick read: %d", errno);
    return atomic_exchange(&kicks, 0);
}

// Waits up to `ms` for a kick. The kicks that came, or 0.
static uint32_t wait_for_kick(uint32_t ms) {
    int fd = atomic_load(&kick_fd);
    uint64_t end = now_us() + (uint64_t)ms * 1000;
    for (uint64_t now = now_us(); now < end; now = now_us()) {
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(fd, &rd);
        if (breathe_select(fd + 1, &rd, NULL, end - now) > 0) return take_kicks(fd);
    }
    return 0;
}

ctrl_state_t ctrl_state(void) {
    return atomic_load(&state);
}

uint32_t ctrl_connections_lost(void) {
    return atomic_load(&lost);
}

void ctrl_describe(char *name, size_t name_size, char *hub, size_t hub_size) {
    portENTER_CRITICAL(&describe_mux);
    strlcpy(name, peer_name, name_size);
    strlcpy(hub, peer_addr, hub_size);
    portEXIT_CRITICAL(&describe_mux);
}

static void set_state(ctrl_state_t s, const char *name, const char *addr) {
    atomic_store(&state, s);
    status_set_conn(s);
    portENTER_CRITICAL(&describe_mux);
    strlcpy(peer_name, name, sizeof peer_name);
    strlcpy(peer_addr, addr, sizeof peer_addr);
    portEXIT_CRITICAL(&describe_mux);
}

// ---- The Connection's hooks. They run inside the ctrl task.

static void on_token(void *ctx, const char *token) {
    (void)ctx;
    if (token) ESP_LOGI(TAG, "paired: storing the token");
    else ESP_LOGW(TAG, "the hub refused our token: wiping it, to pair again");
    config_set_token(token);
}

static void on_command(void *ctx, const tas_msg_t *cmd, tas_arena_t *arena, tas_msg_t *reply) {
    (void)ctx;
    commands_handle(cmd, arena, reply);
}

static const tas_conn_hooks_t hooks = {.command = on_command, .token = on_token};

// ---- One Connection

static int open_udp(uint16_t *port) {
    int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_ANY)};
    socklen_t len = sizeof addr;
    if (s < 0 || bind(s, (struct sockaddr *)&addr, sizeof addr) < 0 ||
        getsockname(s, (struct sockaddr *)&addr, &len) < 0) {
        ESP_LOGE(TAG, "UDP socket: %d", errno);
        if (s >= 0) close(s);
        return -1;
    }
    *port = ntohs(addr.sin_port);
    return s;
}

// Connects without blocking for longer than the watchdog allows. -1 if it can't.
static int open_tcp(const struct sockaddr_in *hub) {
    int s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s < 0) {
        ESP_LOGE(TAG, "TCP socket: %d", errno);
        return -1;
    }
    int one = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
    if (connect(s, (const struct sockaddr *)hub, sizeof *hub) < 0 && errno != EINPROGRESS) {
        ESP_LOGW(TAG, "connect: %d", errno);
        close(s);
        return -1;
    }
    uint64_t end = now_us() + CONNECT_TIMEOUT_US;
    for (uint64_t now = now_us(); now < end; now = now_us()) {
        fd_set wr;
        FD_ZERO(&wr);
        FD_SET(s, &wr);
        if (breathe_select(s + 1, NULL, &wr, end - now) > 0) {
            int err = 0;
            socklen_t len = sizeof err;
            getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len);
            if (!err) return s;
            ESP_LOGW(TAG, "connect: %d", err);
            close(s);
            return -1;
        }
    }
    ESP_LOGW(TAG, "connect timed out");
    close(s);
    return -1;
}

static void flush(int tcp) {
    size_t size;
    const uint8_t *out = tas_conn_outbox(&conn, &size);
    if (!size) return;
    ssize_t n = send(tcp, out, size, 0);
    if (n > 0) {
        tas_conn_sent(&conn, (size_t)n);
    } else if (errno != EAGAIN && errno != EWOULDBLOCK) {
        ESP_LOGW(TAG, "send: %d", errno);
        tas_conn_close(&conn, "send failed");
    }
}

static void receive(int tcp) {
    size_t space;
    uint8_t *to = tas_conn_space(&conn, &space);
    ssize_t n = recv(tcp, to, space, 0);
    if (n > 0) {
        tas_conn_received(&conn, (size_t)n, now_us());
    } else if (n == 0) {
        tas_conn_ended(&conn);
    } else if (errno != EAGAIN && errno != EWOULDBLOCK) {
        ESP_LOGW(TAG, "recv: %d", errno);
        tas_conn_ended(&conn);
    }
}

// Tells the rest of the device what came of the Connection, once per step.
static void track(tas_conn_state_t *seen, tas_backoff_t *backoff, const char *addr) {
    if (conn.state == *seen || conn.state == TAS_CONN_CLOSED) return;
    *seen = conn.state;
    if (*seen == TAS_CONN_PENDING) {
        ESP_LOGI(TAG, "pending: approve device %s on the hub", manifest->device_id.ptr);
        set_state(CTRL_PENDING, "", addr);
    } else if (*seen == TAS_CONN_WELCOMED) {
        ESP_LOGI(TAG, "welcomed as %s", conn.name);
        tas_backoff_reset(backoff);
        set_state(CTRL_WELCOMED, conn.name, addr);
    }
}

// Runs one Connection until it closes. Returns the kicks that ended it, if any.
static uint32_t connection(const struct sockaddr_in *hub, tas_backoff_t *backoff) {
    char addr[HUB_ADDR_MAX];
    inet_ntoa_r(hub->sin_addr, addr, sizeof addr);
    snprintf(addr + strlen(addr), sizeof addr - strlen(addr), ":%u", ntohs(hub->sin_port));
    uint16_t udp_port;
    int udp = open_udp(&udp_port);
    if (udp < 0) return 0;
    int tcp = open_tcp(hub);
    if (tcp < 0) {
        close(udp);
        return 0;
    }
    config_t config = config_get();
    tas_conn_open(&conn, manifest, config.has_token ? config.token : NULL, udp_port, &hooks,
                  now_us());
    ESP_LOGI(TAG, "connected to %s, hello sent%s", addr, config.has_token ? "" : " (unpaired)");
    set_state(CTRL_HELLO, "", addr);
    int kick = atomic_load(&kick_fd);
    tas_conn_state_t seen = TAS_CONN_HELLO;
    uint32_t kicked = 0;
    while (conn.state != TAS_CONN_CLOSED) {
        flush(tcp);
        if (conn.state == TAS_CONN_CLOSED) break;
        size_t pending;
        tas_conn_outbox(&conn, &pending);
        fd_set rd, wr;
        FD_ZERO(&rd);
        FD_ZERO(&wr);
        FD_SET(tcp, &rd);
        FD_SET(kick, &rd);
        if (pending) FD_SET(tcp, &wr);
        uint64_t now = now_us(), deadline = tas_conn_deadline(&conn);
        int n = breathe_select((tcp > kick ? tcp : kick) + 1, &rd, &wr,
                               deadline > now ? deadline - now : 0);
        if (n < 0 && errno != EINTR) {
            ESP_LOGE(TAG, "select: %d", errno);
            tas_conn_close(&conn, "select failed");
            break;
        }
        if (n > 0 && FD_ISSET(kick, &rd)) {
            kicked = take_kicks(kick);
            if (kicked & CTRL_KICK_RECONNECT) tas_conn_close(&conn, "settings changed");
            else if (kicked & CTRL_KICK_WIFI_DOWN) tas_conn_close(&conn, "WiFi lost");
            if (conn.state == TAS_CONN_CLOSED) break;
        }
        if (n > 0 && FD_ISSET(tcp, &rd)) receive(tcp);
        tas_conn_tick(&conn, now_us());
        track(&seen, backoff, addr);
    }
    ESP_LOGW(TAG, "Connection closed: %s", conn.why);
    tas_conn_close(&conn, "closed"); // frees what it holds; `why` stays
    close(tcp);
    close(udp);
    if (seen == TAS_CONN_WELCOMED) atomic_fetch_add(&lost, 1);
    set_state(CTRL_IDLE, "", "");
    return kicked;
}

static void ctrl_task(void *arg) {
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    tas_backoff_t backoff = {0};
    struct sockaddr_in hub;
    bool resolve = true;
    for (;;) {
        while (!net_up()) {
            if (wait_for_kick(BREATHE_US / 1000) & CTRL_KICK_RECONNECT) resolve = true;
        }
        uint32_t kicked = 0;
        if (!resolve || net_find_hub(&hub)) {
            resolve = false;
            kicked = connection(&hub, &backoff);
        }
        if (!(kicked & CTRL_KICK_RECONNECT)) {
            tas_retry_t retry = tas_backoff_failed(&backoff, esp_random());
            resolve = resolve || retry.resolve;
            ESP_LOGI(TAG, "retrying in %u ms%s", (unsigned)retry.delay_ms,
                     resolve ? ", finding the hub again" : "");
            kicked = wait_for_kick(retry.delay_ms);
        }
        if (kicked & CTRL_KICK_RECONNECT) {
            // New settings: find the hub again, at once.
            resolve = true;
            tas_backoff_reset(&backoff);
        }
    }
}

void ctrl_start(const tas_manifest_t *m) {
    manifest = m;
    esp_vfs_eventfd_config_t config = ESP_VFS_EVENTD_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_vfs_eventfd_register(&config));
    int fd = eventfd(0, 0);
    if (fd < 0) abort();
    atomic_store(&kick_fd, fd);
    xTaskCreatePinnedToCore(ctrl_task, "ctrl", CTRL_STACK, NULL, CTRL_PRIORITY, NULL, CTRL_CORE);
}
