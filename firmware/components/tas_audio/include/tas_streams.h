#pragma once
// Stream ids (docs/protocol/audio.md, "Stream ids"): 0 is invalid, the hub assigns 1-127 and the
// device 128-255.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TAS_STREAM_HUB_FIRST 1
#define TAS_STREAM_HUB_LAST 127
#define TAS_STREAMS_CLOSED 16

// The last 16 stream ids closed on a Connection: their late frames are dropped silently. Not
// thread-safe: the caller serializes.
typedef struct {
    uint8_t ids[TAS_STREAMS_CLOSED];
    size_t count, next;
} tas_closed_t;

// Forgets every id: a new Connection.
void tas_closed_clear(tas_closed_t *closed);
void tas_closed_add(tas_closed_t *closed, uint8_t id);
bool tas_closed_has(const tas_closed_t *closed, uint8_t id);
