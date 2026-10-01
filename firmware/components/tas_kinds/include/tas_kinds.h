#pragma once
// The Kind modules: one board-agnostic module per Kind. Each owns its Kind's whole contract
// (docs/protocol/kinds/): commands, events, streams and stats. A Board may have several
// capabilities of one Kind, so a module runs one instance per capability.

#include "tas_board.h"
#include "tas_json.h"
#include "tas_led.h"
#include "tas_wire.h"

typedef struct {
    const char *name;                   // the Kind in the Manifest
    void (*init)(const tas_cap_t *cap); // sets up one instance
    // The capability's Manifest params, in `arena`. NULL for none, or when the arena runs out.
    tas_json_t *(*params)(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena);
    // Answers a command to `cap`, as tas_conn_hooks_t.command does. Runs in the ctrl task, so it
    // must not block.
    void (*command)(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply);
    // The Connection ended, and with it whatever of `cap`'s belonged to it.
    void (*disconnected)(const tas_cap_t *cap);
} tas_kind_module_t;

const tas_kind_module_t *tas_kind_module(tas_kind_t kind);

// What the agent's claim on the led capability `cap` shows now, for the led task to render.
void tas_kinds_led(const tas_cap_t *cap, tas_led_t *out);
// The volume of the speaker capability `cap`, 0-100.
uint8_t tas_kinds_volume(const tas_cap_t *cap);
