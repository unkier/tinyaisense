#pragma once
// The device↔hub wire codec (docs/protocol/device.md): TCP frames, the JSON control messages and
// UDP datagrams, in both directions. Sans-IO and pure C: the golden vectors run against it on the
// PC (docs/protocol/vectors/).

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tas_json.h"

#define TAS_PROTOCOL 1
#define TAS_DEVICE_PORT 8420
#define TAS_FRAME_HEADER 4
#define TAS_MAX_FRAME_BODY 4096
#define TAS_FRAME_JSON 1
#define TAS_UDP_HEADER 20
#define TAS_MAX_UDP_PAYLOAD 1400
#define TAS_UDP_FLAG_ENCRYPTED 0x8000
#define TAS_MAX_TOKEN 64

typedef enum {
    TAS_WIRE_OK,
    // The decode errors of device.md.
    TAS_WIRE_FRAME_TOO_LARGE,
    TAS_WIRE_TRUNCATED,
    TAS_WIRE_BAD_JSON,
    TAS_WIRE_BAD_MESSAGE,
    TAS_WIRE_PROTOCOL_MISMATCH,
    TAS_WIRE_SHORT_DATAGRAM,
    TAS_WIRE_DATAGRAM_TOO_LARGE,
    TAS_WIRE_ENCRYPTED,
    TAS_WIRE_BAD_STREAM,
    TAS_WIRE_PARTIAL_SAMPLE,
    TAS_WIRE_SHORT_FRAME,
    TAS_WIRE_OVERSIZED_FRAME,
    // Not a protocol error: the arena ran out.
    TAS_WIRE_NO_MEMORY,
} tas_wire_err_t;

// The error's code as device.md names it, e.g. "bad_json".
const char *tas_wire_err_name(tas_wire_err_t err);

// ---- TCP frames

typedef struct {
    uint8_t type, flags;
    uint16_t len;
    const uint8_t *body; // valid until the next tas_framer_space
} tas_frame_t;

// Splits a TCP byte stream into frames, whatever the chunking. The caller reads into the space
// it offers, commits what it read, then takes frames until there are none. The space always has
// room for the rest of the frame in progress.
typedef struct {
    uint8_t buf[TAS_FRAME_HEADER + TAS_MAX_FRAME_BODY];
    size_t len;  // bytes held
    size_t head; // bytes of buf already taken as frames
} tas_framer_t;

void tas_framer_reset(tas_framer_t *f);
uint8_t *tas_framer_space(tas_framer_t *f, size_t *size);
void tas_framer_commit(tas_framer_t *f, size_t size);
// TAS_WIRE_OK with *got set when a frame is out. On an error the stream is broken for good.
tas_wire_err_t tas_framer_next(tas_framer_t *f, tas_frame_t *frame, bool *got);
// The stream ended: TAS_WIRE_TRUNCATED if it ended inside a frame.
tas_wire_err_t tas_framer_close(const tas_framer_t *f);

// ---- JSON messages

typedef enum {
    TAS_MSG_NONE, // a message of a type this codec doesn't know: skip it
    TAS_MSG_HELLO,
    TAS_MSG_WELCOME,
    TAS_MSG_PENDING,
    TAS_MSG_ERROR,
    TAS_MSG_CMD,
    TAS_MSG_REPLY,
    TAS_MSG_EVENT,
    TAS_MSG_PING,
    TAS_MSG_PONG,
} tas_msg_type_t;

typedef struct {
    tas_str_t id, kind;
    const tas_json_t *params; // an object; NULL or empty is left out
} tas_wire_cap_t;

typedef struct {
    uint32_t protocol;
    tas_str_t device_id, board, firmware;
    const tas_wire_cap_t *caps;
    size_t cap_count;
    const tas_json_t *exclusive; // an array of arrays of capability ids
} tas_manifest_t;

typedef struct {
    tas_msg_type_t type;
    union {
        struct {
            bool has_token;
            tas_str_t token;
            uint16_t udp_port;
            tas_manifest_t manifest;
        } hello;
        struct {
            tas_str_t name;
            uint32_t conn_id;
            uint16_t udp_port;
            bool has_token; // only on first Pairing
            tas_str_t token;
        } welcome;
        struct {
            tas_str_t code, message;
            uint32_t protocol;
        } error;
        struct {
            uint32_t id;
            bool has_cap; // false for a device-level command
            tas_str_t cap, name;
            const tas_json_t *args;
        } cmd;
        struct {
            uint32_t id;
            bool ok;
            const tas_json_t *result; // when ok
            tas_str_t code, message;  // when not ok
        } reply;
        struct {
            uint32_t seq;
            tas_str_t cap, name;
            uint64_t ts;
            const tas_json_t *data;
        } event;
        struct {
            uint64_t t;
        } ping;
        struct {
            uint64_t t, ts;
        } pong;
    };
} tas_msg_t;

// Decodes one JSON body. The message lives in `arena`.
tas_wire_err_t tas_msg_decode(const uint8_t *body, size_t len, tas_arena_t *arena, tas_msg_t *msg);

// Encodes `msg` as canonical JSON. The length, or -1 if it doesn't fit in `cap`.
int tas_msg_encode(const tas_msg_t *msg, uint8_t *buf, size_t cap);

// Encodes `msg` as one type-1 frame. The length, or -1 if it doesn't fit in `cap` or its body is
// over the frame limit.
int tas_msg_frame(const tas_msg_t *msg, uint8_t *buf, size_t cap);

// ---- UDP datagrams

#define TAS_DGRAM_STREAM 1
#define TAS_DGRAM_MIRROR 2

typedef struct {
    uint32_t conn_id;
    uint8_t kind, stream;
    uint16_t flags;
    uint32_t seq;
    uint64_t ts;
    const uint8_t *payload; // points into the datagram
    size_t len;
} tas_dgram_t;

// Checks the header rules. A kind other than stream or mirror decodes, for the caller to ignore.
tas_wire_err_t tas_dgram_decode(const uint8_t *data, size_t len, tas_dgram_t *dgram);
// Header and payload. The length, or -1 if it doesn't fit in `cap`.
int tas_dgram_encode(const tas_dgram_t *dgram, uint8_t *buf, size_t cap);

// The event a mirror carries, checked against the mirror's header.
tas_wire_err_t tas_mirror_decode(const tas_dgram_t *dgram, tas_arena_t *arena, tas_msg_t *event);
// A mirror of `event` (a TAS_MSG_EVENT). The length, or -1 if it doesn't fit.
int tas_mirror_encode(uint32_t conn_id, uint16_t flags, const tas_msg_t *event, uint8_t *buf,
                      size_t cap);

// The frame rules of docs/protocol/audio.md, for a pcm_s16le stream frame of `size` bytes.
tas_wire_err_t tas_frame_check(size_t size, uint32_t rate, uint32_t channels, uint32_t frame_ms,
                               bool final);

// ---- Command args

// Reading a command's args by its contract (packages/wire/src/tinyaisense_wire/contracts/): a
// member of the wrong type or out of range breaks it, and the reply is invalid_args. Unknown
// members are ignored.
typedef enum {
    TAS_ARG_ABSENT,
    TAS_ARG_OK,
    TAS_ARG_BAD,
} tas_arg_t;

// The member `key` of `args`: an integer in [min, max]. `1.0` and `true` aren't integers.
tas_arg_t tas_arg_int(const tas_json_t *args, const char *key, int64_t min, int64_t max,
                      int64_t *out);
// The member `key` of `args`: one of the `count` strings in `names`, as its index.
tas_arg_t tas_arg_enum(const tas_json_t *args, const char *key, const char *const *names,
                       size_t count, size_t *out);

// Fails the reply `reply` with `code` and `message`, which must outlive it: literals.
void tas_reply_fail(tas_msg_t *reply, const char *code, const char *message);
