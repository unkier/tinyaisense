#pragma once
// One Connection to the hub, sans-IO (docs/protocol/device.md): the handshake, keepalive,
// protocol errors and replies. The ctrl task owns the socket: it opens the Connection once TCP is
// up, reads into its space, sends what the outbox holds, ticks it by the deadline, and closes the
// socket once the Connection is closed. Pure C, no IDF calls.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tas_wire.h"

#define TAS_CONN_DEAD_US 6000000 // no inbound frame for this long: the Connection is dead
#define TAS_CONN_OUTBOX (2 * (TAS_FRAME_HEADER + TAS_MAX_FRAME_BODY))
#define TAS_CONN_ARENA_LIMIT (32 * 1024) // the most one inbound message may take
#define TAS_CONN_MAX_NAME 63

typedef enum {
    TAS_CONN_HELLO,    // hello sent, no answer yet
    TAS_CONN_PENDING,  // waiting for the owner to approve
    TAS_CONN_WELCOMED, // paired, and the hub's commands flow
    TAS_CONN_CLOSED,   // over: close the socket
} tas_conn_state_t;

typedef struct {
    void *ctx;
    // Answers a Command. It runs inside tas_conn_received, so it must not block. `reply` comes in
    // as `ok` with an empty result, both in `arena`; set `ok` false and the error code and message
    // to fail it. Strings it sets must outlive the call: literals, or the arena.
    void (*command)(void *ctx, const tas_msg_t *cmd, tas_arena_t *arena, tas_msg_t *reply);
    // Pairing issued `token`: store it. NULL: the hub refused the token sent, so wipe it.
    void (*token)(void *ctx, const char *token);
} tas_conn_hooks_t;

typedef struct {
    tas_conn_state_t state;
    const char *why; // why it closed, for the log
    // From welcome:
    char name[TAS_CONN_MAX_NAME + 1]; // cut short if longer
    uint32_t conn_id;
    uint16_t hub_udp_port;

    tas_conn_hooks_t hooks;
    uint64_t last_rx_us;
    tas_framer_t framer;
    tas_arena_t arena;
    uint8_t out[TAS_CONN_OUTBOX];
    size_t out_len;
} tas_conn_t;

// TCP is up: starts a Connection and queues its hello. `token` is NULL when unpaired.
void tas_conn_open(tas_conn_t *conn, const tas_manifest_t *manifest, const char *token,
                   uint16_t udp_port, const tas_conn_hooks_t *hooks, uint64_t now_us);

// Where to read inbound bytes to, and how many fit.
uint8_t *tas_conn_space(tas_conn_t *conn, size_t *size);
// `size` bytes were read into the space: handles every complete frame, at device time `now_us`.
void tas_conn_received(tas_conn_t *conn, size_t size, uint64_t now_us);
// The hub closed the stream.
void tas_conn_ended(tas_conn_t *conn);
// Checks the keepalive. Call it by tas_conn_deadline at the latest.
void tas_conn_tick(tas_conn_t *conn, uint64_t now_us);
uint64_t tas_conn_deadline(const tas_conn_t *conn);

// The bytes waiting to go out, and how many of them went.
const uint8_t *tas_conn_outbox(const tas_conn_t *conn, size_t *size);
void tas_conn_sent(tas_conn_t *conn, size_t size);

// Closes the Connection for `why`, unless it's closed already, and frees what it holds.
void tas_conn_close(tas_conn_t *conn, const char *why);
