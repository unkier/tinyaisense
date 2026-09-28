#pragma once
// The Kind modules: one board-agnostic module per Kind. Each owns its Kind's whole contract
// (docs/protocol/kinds/): commands, events, streams and stats. A Board may have several
// capabilities of one Kind, so a module runs one instance per capability.

#include "tas_board.h"

typedef struct {
    const char *name;                  // the Kind in the Manifest
    void (*init)(const tas_cap_t *cap); // sets up one instance
} tas_kind_module_t;

const tas_kind_module_t *tas_kind_module(tas_kind_t kind);
