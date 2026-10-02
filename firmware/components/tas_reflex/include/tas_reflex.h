#pragma once
// One button's Reflex (docs/protocol/kinds/button.md, ADR 0003), sans-IO: what the button sends on
// its edges, and what an armed one does by itself. On down it cuts its speakers, then opens its
// mic on a device-range stream id; on up it closes only a mic it opened. The button Kind module
// drives it with the confirmed edges, the hub's set_reflex and the end of each Connection, from
// one task at a time; the device's speakers, mics and events are behind `ops`. Pure C, no IDF
// calls.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tas_board.h"
#include "tas_debounce.h"
#include "tas_jbuf.h"
#include "tas_wire.h"

#define TAS_REFLEX_MAX_STOP 8 // speakers a set_reflex may name
#define TAS_REFLEX_STREAM_FIRST 128
#define TAS_REFLEX_STREAM_LAST 255

// How a mic's stream ended.
typedef struct {
    bool any;          // a frame went out
    uint32_t last_seq; // the last one's, if any
    uint32_t dropped;  // frames its queue dropped
} tas_mic_end_t;

typedef struct {
    void *ctx;
    // Cuts the speaker `cap` by DMA patch, timed from the edge at `edge_us`. False if it had no
    // stream open; else that stream's id, and what was heard of it.
    bool (*cut)(void *ctx, const tas_cap_t *cap, uint64_t edge_us, uint8_t *stream,
                tas_jbuf_stats_t *heard);
    // The id of the stream the mic `cap` has open. False if none.
    bool (*mic_stream)(void *ctx, const tas_cap_t *cap, uint8_t *stream);
    // Opens the mic `cap` on the device-range `stream`, with `frame_ms` frames. False if it can't:
    // a stream is open, or an exclusive peer is active.
    bool (*mic_open)(void *ctx, const tas_cap_t *cap, uint8_t stream, uint32_t frame_ms);
    // Closes the mic `cap`'s stream `stream`, once its last frame has gone out. False if it has no
    // stream of that id open, e.g. the hub stopped it.
    bool (*mic_close)(void *ctx, const tas_cap_t *cap, uint8_t stream, tas_mic_end_t *end);
    // Queues the event `name` of the capability `cap`, its data `len` bytes of JSON, and with
    // `mirror` its UDP mirror. False if no Connection is welcomed: then it's dropped.
    bool (*emit)(void *ctx, const char *cap, const char *name, uint64_t ts, const uint8_t *data,
                 size_t len, bool mirror);
} tas_reflex_ops_t;

typedef struct {
    const tas_board_t *board;
    const tas_cap_t *button;
    const uint32_t *frame_ms; // the frame durations the mics advertise
    size_t frame_ms_count;
    tas_reflex_ops_t ops;
} tas_reflex_config_t;

typedef struct {
    tas_reflex_config_t cfg;
    // Armed by the hub:
    bool armed;
    const tas_cap_t *stop[TAS_REFLEX_MAX_STOP];
    size_t stop_count;
    const tas_cap_t *start; // the mic to open, or NULL
    uint32_t start_frame_ms;
    // This Connection's:
    bool pressed;                // its down went out, so its up goes out too
    const tas_cap_t *opened_mic; // the start mic, if the press opened it
    uint8_t opened_stream;
    uint8_t last_stream;         // the device-range id used last
} tas_reflex_t;

// A button whose Reflex is disarmed.
void tas_reflex_init(tas_reflex_t *r, const tas_reflex_config_t *cfg);
// Runs the hub's `set_reflex{armed, stop?, start?}`. `reply` comes in ok; args that break the
// contract, name a stop that isn't a speaker or a start that isn't a mic, or a format the mic
// doesn't advertise, fail it with invalid_args and leave the Reflex as it was.
void tas_reflex_set(tas_reflex_t *r, const tas_json_t *args, tas_msg_t *reply);
// The button's confirmed `edge`, handled at device time `now_us`. A down goes out only while a
// Connection is welcomed, and its up only on the same Connection.
void tas_reflex_edge(tas_reflex_t *r, const tas_edge_t *edge, uint64_t now_us);
// The Connection ended: the Reflex disarms, and a button held now is ignored until it's let go.
// What the Connection's streams were, the speakers and mics end themselves.
void tas_reflex_disconnected(tas_reflex_t *r);
