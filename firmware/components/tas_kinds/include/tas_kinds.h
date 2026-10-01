#pragma once
// The Kind modules: one board-agnostic module per Kind. Each owns its Kind's whole contract
// (docs/protocol/kinds/): commands, events, streams and stats. A Board may have several
// capabilities of one Kind, so a module runs one instance per capability.

#include "sdkconfig.h"
#include "tas_board.h"
#include "tas_json.h"
#include "tas_led.h"
#include "tas_wire.h"

#define TAS_KIND_EVENT_DATA 128 // bytes of an event's data, as JSON
#define TAS_KINDS_MAX_FRAME 320 // samples: the longest frame advertised, 20 ms at 16 kHz

// An event a Kind module emits: the data object, already JSON.
typedef struct {
    const char *cap, *name; // literals, or a capability id
    uint64_t ts;            // device us
    size_t len;
    uint8_t data[TAS_KIND_EVENT_DATA];
} tas_kind_event_t;

// What the Kind modules need of the core. Each may be called from any task.
typedef struct {
    // Queues `event` for the Connection. Never blocks: with none welcomed, or the queue full, it's
    // dropped.
    void (*emit)(const tas_kind_event_t *event);
    // A mic has queued a frame: wake the sender.
    void (*frames_ready)(void);
    // Sends every mic frame queued now, in order, and returns once they're out: a few ms.
    void (*send_frames)(void);
} tas_kinds_core_t;

// What became of a stream frame from the hub.
typedef enum {
    TAS_FRAME_TAKEN,
    TAS_FRAME_LATE,    // its stream dropped it: late, a duplicate, or past the drain
    TAS_FRAME_BAD,     // it breaks the frame rules
    TAS_FRAME_CLOSED,  // its stream closed lately: dropped silently
    TAS_FRAME_UNKNOWN, // no stream of its id is open
} tas_frame_result_t;

// A mic frame for the hub.
typedef struct {
    uint8_t stream;
    uint32_t seq;
    uint64_t ts; // device us of its first sample
    size_t samples;
    int16_t pcm[TAS_KINDS_MAX_FRAME];
} tas_mic_frame_t;

typedef struct {
    const char *name;                   // the Kind in the Manifest
    // Sets up one instance, before any task runs.
    void (*init)(const tas_board_t *board, const tas_cap_t *cap);
    // The capability's Manifest params, in `arena`. NULL for none, or when the arena runs out.
    tas_json_t *(*params)(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena);
    // Answers a command to `cap`, as tas_conn_hooks_t.command does. Runs in the ctrl task, so it
    // must not block for long: a mic's stop waits a few ms for its last frames to go out.
    void (*command)(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply);
    // The Connection ended, and with it whatever of `cap`'s belonged to it.
    void (*disconnected)(const tas_cap_t *cap);
    // Hands `frame`, a stream frame from the hub, to `cap`: TAS_FRAME_UNKNOWN if `cap` has no
    // stream of its id open. NULL if the Kind streams nothing down.
    tas_frame_result_t (*frame_in)(const tas_cap_t *cap, const tas_dgram_t *frame,
                                   tas_wire_err_t *err);
    // Takes the oldest frame `cap` has queued for the hub. NULL if the Kind streams nothing up.
    bool (*frame_out)(const tas_cap_t *cap, tas_mic_frame_t *frame);
    // Whether `cap` makes no sound, nor will until a stream opens, so an exclusive peer may take
    // the pins they share. NULL if it never does.
    bool (*quiet)(const tas_cap_t *cap);
} tas_kind_module_t;

const tas_kind_module_t *tas_kind_module(tas_kind_t kind);

// Sets up every capability of `board`, before any task runs. `core` must outlive the device.
void tas_kinds_init(const tas_board_t *board, const tas_kinds_core_t *core);
// The Connection ended: ends what of each capability's belonged to it, e.g. an LED claim or a
// stream, and forgets its closed stream ids.
void tas_kinds_disconnected(void);
// Whether a stream of `kind` is open, for the Status.
bool tas_kinds_streaming(tas_kind_t kind);

// Hands `frame`, a stream frame from the hub whose payload is 2-byte aligned, to the stream it
// belongs to. `err` says why a TAS_FRAME_BAD one is.
tas_frame_result_t tas_kinds_frame_in(const tas_dgram_t *frame, tas_wire_err_t *err);
// Takes the oldest frame a mic has queued. False if none has any.
bool tas_kinds_frame_out(tas_mic_frame_t *frame);

// What the agent's claim on the led capability `cap` shows now, for the led task to render.
void tas_kinds_led(const tas_cap_t *cap, tas_led_t *out);
// The volume of the speaker capability `cap`, 0-100.
uint8_t tas_kinds_volume(const tas_cap_t *cap);

#if CONFIG_TAS_TEST_HOOKS
// Registers the debug builds' console commands. Call it once the console is initialized.
void tas_kinds_test_commands(const tas_board_t *board);
#endif
