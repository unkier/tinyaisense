// The serial console. It reads lines itself, rather than with linenoise, so that its task can
// feed the watchdog while it waits.

#include <stdio.h>
#include <string.h>

#include "core.h"
#include "driver/uart.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tas_conn.h"
#include "tas_kinds.h"

#define TAG "console"
#define UART CONFIG_ESP_CONSOLE_UART_NUM
#define CONSOLE_PRIORITY 2
#define CONSOLE_STACK 4096
#define MAX_LINE 160
#define PROMPT "tas> "

static const tas_board_t *board;
static const tas_manifest_t *manifest;

static void restart(void) {
    printf("rebooting\n");
    fflush(stdout);
    vTaskDelay(pdMS_TO_TICKS(100));
    esp_restart();
}

static int cmd_wifi(int argc, char **argv) {
    if (argc < 2 || argc > 3) {
        printf("usage: wifi <ssid> [password]\n");
        return 1;
    }
    const char *pass = argc == 3 ? argv[2] : "";
    if (strlen(argv[1]) > WIFI_SSID_MAX || strlen(pass) > WIFI_PASS_MAX) {
        printf("the SSID takes up to %d bytes, the password up to %d\n", WIFI_SSID_MAX,
               WIFI_PASS_MAX);
        return 1;
    }
    if (!config_set_wifi(argv[1], pass)) {
        printf("couldn't store it\n");
        return 1;
    }
    restart();
    return 0;
}

static int cmd_hub(int argc, char **argv) {
    if (argc != 2 || strlen(argv[1]) > HUB_MAX || !config_hub_valid(argv[1])) {
        printf("usage: hub <host:port>|auto\n");
        return 1;
    }
    if (!config_set_hub(argv[1])) {
        printf("couldn't store it\n");
        return 1;
    }
    ctrl_kick(CTRL_KICK_RECONNECT);
    return 0;
}

static int cmd_token(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "clear")) {
        printf("usage: token clear\n");
        return 1;
    }
    if (!config_set_token(NULL)) {
        printf("couldn't wipe it\n");
        return 1;
    }
    ctrl_kick(CTRL_KICK_RECONNECT);
    return 0;
}

static const char *conn_names[] = {
    [CTRL_IDLE] = "none",
    [CTRL_HELLO] = "hello sent",
    [CTRL_PENDING] = "pending: approve it on the hub",
    [CTRL_WELCOMED] = "welcomed",
};

static int cmd_status(int argc, char **argv) {
    (void)argc;
    (void)argv;
    config_t c = config_get();
    char ip[16], name[TAS_CONN_MAX_NAME + 1], hub[HUB_ADDR_MAX];
    net_ip(ip, sizeof ip);
    ctrl_describe(name, sizeof name, hub, sizeof hub);
    printf("device    %.*s, %s, firmware %.*s\n", (int)manifest->device_id.len,
           manifest->device_id.ptr, board->name, (int)manifest->firmware.len,
           manifest->firmware.ptr);
    if (!c.ssid[0]) printf("wifi      not set\n");
    else if (net_up()) printf("wifi      %s, %s, %d dBm\n", c.ssid, ip, net_rssi());
    else printf("wifi      %s, down\n", c.ssid);
    printf("hub       %s%s%s\n", c.hub, hub[0] ? ", at " : "", hub);
    printf("conn      %s%s%s, %u lost\n", conn_names[ctrl_state()], name[0] ? " as " : "", name,
           (unsigned)ctrl_connections_lost());
    printf("paired    %s\n", c.has_token ? "yes" : "no");
    printf("status    %s\n", tas_status_name(status_shown()));
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        if (cap->kind != TAS_KIND_SPEAKER) continue;
        printf("volume    %u, %s\n", tas_kinds_volume(cap), cap->id);
    }
    printf("heap      %u B free, %u B lowest\n", (unsigned)esp_get_free_heap_size(),
           (unsigned)esp_get_minimum_free_heap_size());
    printf("uptime    %llu s\n", (unsigned long long)(esp_timer_get_time() / 1000000));
    return 0;
}

static int cmd_reboot(int argc, char **argv) {
    (void)argc;
    (void)argv;
    restart();
    return 0;
}

static const esp_console_cmd_t commands[] = {
    {.command = "wifi", .help = "Store the WiFi to join, then reboot", .hint = "<ssid> [password]",
     .func = cmd_wifi},
    {.command = "hub", .help = "Where the hub is: auto for mDNS", .hint = "<host:port>|auto",
     .func = cmd_hub},
    {.command = "token", .help = "Wipe the pairing token, to pair again", .hint = "clear",
     .func = cmd_token},
    {.command = "status", .help = "Show the device, WiFi, hub and Connection", .func = cmd_status},
    {.command = "reboot", .help = "Reboot", .func = cmd_reboot},
};

static void run(const char *line) {
    int ret;
    esp_err_t err = esp_console_run(line, &ret);
    if (err == ESP_ERR_NOT_FOUND) printf("unknown command; try help\n");
    else if (err && err != ESP_ERR_INVALID_ARG) printf("%s\n", esp_err_to_name(err));
}

static void console_task(void *arg) {
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    char line[MAX_LINE + 1];
    size_t len = 0;
    printf("\n" PROMPT);
    fflush(stdout);
    for (;;) {
        uint8_t c;
        int n = uart_read_bytes(UART, &c, 1, pdMS_TO_TICKS(1000));
        core_breathe();
        if (n <= 0) continue;
        if (c == '\r' || c == '\n') {
            if (!len) continue; // the other half of a CRLF
            printf("\n");
            line[len] = 0;
            len = 0;
            run(line);
            printf(PROMPT);
        } else if ((c == '\b' || c == 0x7f) && len) {
            len--;
            printf("\b \b");
        } else if (c >= ' ' && c != 0x7f && len < MAX_LINE) {
            line[len++] = (char)c;
            putchar(c);
        }
        fflush(stdout);
    }
}

void console_start(const tas_board_t *b, const tas_manifest_t *m) {
    board = b;
    manifest = m;
    ESP_ERROR_CHECK(uart_driver_install(UART, 256, 0, 0, NULL, 0));
    esp_console_config_t config = ESP_CONSOLE_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_init(&config));
    ESP_ERROR_CHECK(esp_console_register_help_command());
    for (size_t i = 0; i < sizeof commands / sizeof *commands; i++) {
        ESP_ERROR_CHECK(esp_console_cmd_register(&commands[i]));
    }
    xTaskCreate(console_task, "console", CONSOLE_STACK, NULL, CONSOLE_PRIORITY, NULL);
}
