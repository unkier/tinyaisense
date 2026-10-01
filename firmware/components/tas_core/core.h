#pragma once
// Internal to tas_core: what its parts share.
//
//   config.c    the settings in NVS namespace `tas`, with Kconfig defaults
//   console.c   the serial console
//   net.c       WiFi and finding the hub (mDNS or the static `hub`)
//   ctrl.c      the ctrl task: the Connection, around tas_conn
//   commands.c  the hub's commands
//   status.c    the Status layers, and the led task that renders them
//   manifest.c  the Manifest this device sends

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "lwip/sockets.h"
#include "tas_board.h"
#include "tas_status.h"
#include "tas_wire.h"

// ---- config.c

#define WIFI_SSID_MAX 32 // bytes
#define WIFI_PASS_MAX 64 // a passphrase, or a 64-digit hex key
#define HUB_MAX 71
#define HUB_AUTO "auto"   // the hub setting that finds it over mDNS
#define HUB_ADDR_MAX 22   // "255.255.255.255:65535" and its NUL

typedef struct {
    char ssid[WIFI_SSID_MAX + 1];
    char pass[WIFI_PASS_MAX + 1];
    char hub[HUB_MAX + 1]; // HUB_AUTO, or "host:port"
    bool has_token;
    char token[TAS_MAX_TOKEN + 1];
} config_t;

// Mounts NVS and reads the settings. Kconfig gives the defaults for what NVS lacks.
void config_init(void);
// A copy of the settings now.
config_t config_get(void);
// Each stores one setting. False if NVS failed.
bool config_set_wifi(const char *ssid, const char *pass);
bool config_set_hub(const char *hub);
bool config_set_token(const char *token); // NULL wipes it
// Erases every setting: back to the Kconfig defaults.
bool config_factory_reset(void);

// Splits "host:port". False if it isn't one.
bool config_parse_hub(const char *hub, char *host, size_t host_size, uint16_t *port);
// Whether `hub` is a hub setting: HUB_AUTO, or "host:port".
bool config_hub_valid(const char *hub);

// ---- net.c

// Joins the configured WiFi, and keeps rejoining it. New credentials take a reboot.
void net_start(void);
bool net_up(void);
int net_rssi(void);
void net_ip(char *buf, size_t size);
// Finds the hub: the static `hub` setting, or mDNS. Blocks for seconds, feeding the watchdog.
bool net_find_hub(struct sockaddr_in *hub);

// ---- ctrl.c

typedef enum {
    CTRL_IDLE,    // no Connection
    CTRL_HELLO,   // hello sent
    CTRL_PENDING, // waiting for the owner
    CTRL_WELCOMED,
} ctrl_state_t;

// Why the ctrl task should look up from what it's doing.
#define CTRL_KICK_RECONNECT (1u << 0) // drop the Connection and find the hub again, now
#define CTRL_KICK_WIFI_DOWN (1u << 1) // drop the Connection

void ctrl_start(const tas_manifest_t *manifest);
void ctrl_kick(uint32_t why);
ctrl_state_t ctrl_state(void);
// The hub-given name and its address while there's a Connection, "" otherwise.
void ctrl_describe(char *name, size_t name_size, char *hub, size_t hub_size);
uint32_t ctrl_connections_lost(void);

// ---- commands.c

void commands_init(const tas_board_t *board);
// Answers `cmd` in `reply`, which comes ok with an empty result. Never blocks.
void commands_handle(const tas_msg_t *cmd, tas_arena_t *arena, tas_msg_t *reply);
// The Connection ended: ends what of each capability's belonged to it, e.g. an LED claim.
void commands_disconnected(void);

// ---- status.c

void status_start(const tas_board_t *board);
void status_set_wifi(bool configured, bool up);
void status_set_conn(ctrl_state_t state);
void status_set_hub(tas_status_t layer);
// Shows identify for `s` seconds from now, cutting short any earlier one.
void status_identify(uint32_t s);
tas_status_t status_shown(void);
// Renders `look` on the Status LED once, for use before the led task runs.
void status_render_look(const tas_board_t *board, const tas_look_t *look, uint64_t t_ms);

// ---- console.c

void console_start(const tas_board_t *board, const tas_manifest_t *manifest);

// ---- manifest.c

const tas_manifest_t *manifest_build(const tas_board_t *board);

// ---- core.c

// Feeds the task watchdog, and warns now and then if the heap is low.
void core_breathe(void);
