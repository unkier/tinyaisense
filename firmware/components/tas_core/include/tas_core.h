#pragma once
// The core layer: WiFi, mDNS, framing, the handshake, ping, reconnects, UDP, command dispatch,
// event output and exclusive groups. It runs the Kind modules for the Board's capabilities.

#include "tas_board.h"

void tas_core_start(const tas_board_t *board);
