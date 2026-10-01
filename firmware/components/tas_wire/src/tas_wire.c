#include "tas_wire.h"

#include <string.h>

const char *tas_wire_err_name(tas_wire_err_t err) {
    switch (err) {
    case TAS_WIRE_OK: return "ok";
    case TAS_WIRE_FRAME_TOO_LARGE: return "frame_too_large";
    case TAS_WIRE_TRUNCATED: return "truncated";
    case TAS_WIRE_BAD_JSON: return "bad_json";
    case TAS_WIRE_BAD_MESSAGE: return "bad_message";
    case TAS_WIRE_PROTOCOL_MISMATCH: return "protocol_mismatch";
    case TAS_WIRE_SHORT_DATAGRAM: return "short_datagram";
    case TAS_WIRE_DATAGRAM_TOO_LARGE: return "datagram_too_large";
    case TAS_WIRE_ENCRYPTED: return "encrypted";
    case TAS_WIRE_BAD_STREAM: return "bad_stream";
    case TAS_WIRE_PARTIAL_SAMPLE: return "partial_sample";
    case TAS_WIRE_SHORT_FRAME: return "short_frame";
    case TAS_WIRE_OVERSIZED_FRAME: return "oversized_frame";
    case TAS_WIRE_NO_MEMORY: return "no_memory";
    }
    return "?";
}

static uint16_t get16(const uint8_t *p) {
    return (uint16_t)(p[0] | p[1] << 8);
}

static uint32_t get32(const uint8_t *p) {
    return (uint32_t)get16(p) | (uint32_t)get16(p + 2) << 16;
}

static void put16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static void put32(uint8_t *p, uint32_t v) {
    put16(p, (uint16_t)v);
    put16(p + 2, (uint16_t)(v >> 16));
}

// ---- TCP frames

void tas_framer_reset(tas_framer_t *f) {
    f->len = f->head = 0;
}

uint8_t *tas_framer_space(tas_framer_t *f, size_t *size) {
    if (f->head) {
        memmove(f->buf, f->buf + f->head, f->len - f->head);
        f->len -= f->head;
        f->head = 0;
    }
    *size = sizeof f->buf - f->len;
    return f->buf + f->len;
}

void tas_framer_commit(tas_framer_t *f, size_t size) {
    f->len += size;
}

tas_wire_err_t tas_framer_next(tas_framer_t *f, tas_frame_t *frame, bool *got) {
    *got = false;
    size_t held = f->len - f->head;
    if (held < TAS_FRAME_HEADER) return TAS_WIRE_OK;
    const uint8_t *header = f->buf + f->head;
    uint16_t len = get16(header + 2);
    if (len > TAS_MAX_FRAME_BODY) return TAS_WIRE_FRAME_TOO_LARGE;
    if (held < TAS_FRAME_HEADER + (size_t)len) return TAS_WIRE_OK;
    *frame = (tas_frame_t){header[0], header[1], len, header + TAS_FRAME_HEADER};
    f->head += TAS_FRAME_HEADER + len;
    *got = true;
    return TAS_WIRE_OK;
}

tas_wire_err_t tas_framer_close(const tas_framer_t *f) {
    return f->len > f->head ? TAS_WIRE_TRUNCATED : TAS_WIRE_OK;
}

// ---- Reading message fields. Each is false if the field is missing or mistyped: bad_message.

static bool get_uint(const tas_json_t *obj, const char *key, uint64_t min, uint64_t max,
                     uint64_t *out) {
    const tas_json_t *v = tas_json_get(obj, key);
    if (!v || v->type != TAS_JSON_INT || v->integer < 0) return false;
    *out = (uint64_t)v->integer;
    return *out >= min && *out <= max;
}

static bool get_u32(const tas_json_t *obj, const char *key, uint32_t min, uint32_t *out) {
    uint64_t v;
    if (!get_uint(obj, key, min, UINT32_MAX, &v)) return false;
    *out = (uint32_t)v;
    return true;
}

static bool get_port(const tas_json_t *obj, const char *key, uint16_t *out) {
    uint64_t v;
    if (!get_uint(obj, key, 1, UINT16_MAX, &v)) return false;
    *out = (uint16_t)v;
    return true;
}

static bool get_string(const tas_json_t *obj, const char *key, tas_str_t *out) {
    const tas_json_t *v = tas_json_get(obj, key);
    if (!v || v->type != TAS_JSON_STRING) return false;
    *out = v->text;
    return true;
}

// An optional string: absent is fine, null isn't.
static bool get_optional_string(const tas_json_t *obj, const char *key, bool *has,
                                tas_str_t *out) {
    *has = tas_json_get(obj, key) != NULL;
    return !*has || get_string(obj, key, out);
}

static bool get_typed(const tas_json_t *obj, const char *key, tas_json_type_t type,
                      const tas_json_t **out) {
    const tas_json_t *v = tas_json_get(obj, key);
    if (!v || v->type != type) return false;
    *out = v;
    return true;
}

static bool is_name(tas_str_t s) {
    if (!s.len) return false;
    for (size_t i = 0; i < s.len; i++) {
        char c = s.ptr[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) return false;
    }
    return true;
}

static bool is_device_id(tas_str_t s) {
    if (s.len != 12) return false;
    for (size_t i = 0; i < s.len; i++) {
        char c = s.ptr[i];
        if (!((c >= 'a' && c <= 'f') || (c >= '0' && c <= '9'))) return false;
    }
    return true;
}

static bool is_token(tas_str_t s) {
    if (s.len > TAS_MAX_TOKEN) return false;
    for (size_t i = 0; i < s.len; i++) {
        if ((uint8_t)s.ptr[i] >= 0x80) return false;
    }
    return true;
}

// ---- The Manifest

static bool is_positive_int(const tas_json_t *v) {
    return v && v->type == TAS_JSON_INT && v->integer >= 1;
}

// One of a mic's or speaker's params.formats (contracts/mic.json).
static bool is_format(const tas_json_t *f) {
    const tas_json_t *codec = tas_json_get(f, "codec");
    const tas_json_t *frame_ms = tas_json_get(f, "frame_ms");
    if (f->type != TAS_JSON_OBJECT || !codec || codec->type != TAS_JSON_STRING ||
        !is_positive_int(tas_json_get(f, "rate")) ||
        !is_positive_int(tas_json_get(f, "channels")) || !frame_ms ||
        frame_ms->type != TAS_JSON_ARRAY || !frame_ms->items.count) {
        return false;
    }
    for (const tas_json_t *ms = frame_ms->items.first; ms; ms = ms->next) {
        if (!is_positive_int(ms)) return false;
    }
    return true;
}

// Whether `params` (NULL for none) meet the params schema of `kind`, if it's a Kind we know.
static bool params_fit(tas_str_t kind, const tas_json_t *params) {
    if (tas_str_eq(kind, "mic") || tas_str_eq(kind, "speaker")) {
        const tas_json_t *formats = tas_json_get(params, "formats");
        if (!formats || formats->type != TAS_JSON_ARRAY || !formats->items.count) return false;
        for (const tas_json_t *f = formats->items.first; f; f = f->next) {
            if (!is_format(f)) return false;
        }
        return true;
    }
    if (tas_str_eq(kind, "led")) return is_positive_int(tas_json_get(params, "count"));
    return true; // button params are any object, and unknown Kinds aren't checked
}

static tas_wire_err_t decode_manifest(const tas_json_t *m, tas_arena_t *arena,
                                      tas_manifest_t *out) {
    // The protocol goes first: a mismatch wins over any other problem.
    uint32_t protocol;
    if (!get_u32(m, "protocol", 0, &protocol)) return TAS_WIRE_BAD_MESSAGE;
    if (protocol != TAS_PROTOCOL) return TAS_WIRE_PROTOCOL_MISMATCH;
    out->protocol = protocol;
    const tas_json_t *caps, *exclusive;
    if (!get_string(m, "device_id", &out->device_id) || !is_device_id(out->device_id) ||
        !get_string(m, "board", &out->board) || !get_string(m, "firmware", &out->firmware) ||
        !get_typed(m, "capabilities", TAS_JSON_ARRAY, &caps) ||
        !get_typed(m, "exclusive", TAS_JSON_ARRAY, &exclusive)) {
        return TAS_WIRE_BAD_MESSAGE;
    }
    tas_wire_cap_t *list = tas_arena_alloc(arena, caps->items.count * sizeof *list);
    if (!list) return TAS_WIRE_NO_MEMORY;
    size_t n = 0;
    for (const tas_json_t *c = caps->items.first; c; c = c->next, n++) {
        tas_wire_cap_t *cap = &list[n];
        const tas_json_t *params = tas_json_get(c, "params");
        if (c->type != TAS_JSON_OBJECT || !get_string(c, "id", &cap->id) || !is_name(cap->id) ||
            !get_string(c, "kind", &cap->kind) ||
            (params && params->type != TAS_JSON_OBJECT) || !params_fit(cap->kind, params)) {
            return TAS_WIRE_BAD_MESSAGE;
        }
        cap->params = params;
        for (size_t i = 0; i < n; i++) {
            if (tas_str_same(list[i].id, cap->id)) return TAS_WIRE_BAD_MESSAGE;
        }
    }
    for (const tas_json_t *group = exclusive->items.first; group; group = group->next) {
        if (group->type != TAS_JSON_ARRAY) return TAS_WIRE_BAD_MESSAGE;
        for (const tas_json_t *id = group->items.first; id; id = id->next) {
            bool known = false;
            for (size_t i = 0; id->type == TAS_JSON_STRING && i < n && !known; i++) {
                known = tas_str_same(list[i].id, id->text);
            }
            if (!known) return TAS_WIRE_BAD_MESSAGE;
        }
    }
    out->caps = list;
    out->cap_count = n;
    out->exclusive = exclusive;
    return TAS_WIRE_OK;
}

// ---- Messages

static tas_wire_err_t decode_hello(const tas_json_t *root, tas_arena_t *arena, tas_msg_t *msg) {
    const tas_json_t *manifest;
    if (!get_typed(root, "manifest", TAS_JSON_OBJECT, &manifest)) return TAS_WIRE_BAD_MESSAGE;
    tas_wire_err_t err = decode_manifest(manifest, arena, &msg->hello.manifest);
    if (err) return err;
    const tas_json_t *token = tas_json_get(root, "token");
    if (!token || (token->type != TAS_JSON_NULL && token->type != TAS_JSON_STRING)) {
        return TAS_WIRE_BAD_MESSAGE;
    }
    msg->hello.has_token = token->type == TAS_JSON_STRING;
    if (msg->hello.has_token) {
        msg->hello.token = token->text;
        if (!is_token(token->text)) return TAS_WIRE_BAD_MESSAGE;
    }
    return get_port(root, "udp_port", &msg->hello.udp_port) ? TAS_WIRE_OK : TAS_WIRE_BAD_MESSAGE;
}

static bool decode_welcome(const tas_json_t *root, tas_msg_t *msg) {
    return get_string(root, "name", &msg->welcome.name) && is_name(msg->welcome.name) &&
           get_u32(root, "conn_id", 1, &msg->welcome.conn_id) &&
           get_port(root, "udp_port", &msg->welcome.udp_port) &&
           get_optional_string(root, "token", &msg->welcome.has_token, &msg->welcome.token) &&
           (!msg->welcome.has_token || is_token(msg->welcome.token));
}

static bool decode_reply(const tas_json_t *root, tas_msg_t *msg) {
    const tas_json_t *ok = tas_json_get(root, "ok"), *error;
    if (!get_u32(root, "id", 0, &msg->reply.id) || !ok ||
        (ok->type != TAS_JSON_TRUE && ok->type != TAS_JSON_FALSE)) {
        return false;
    }
    msg->reply.ok = ok->type == TAS_JSON_TRUE;
    if (msg->reply.ok) return get_typed(root, "result", TAS_JSON_OBJECT, &msg->reply.result);
    return get_typed(root, "error", TAS_JSON_OBJECT, &error) &&
           get_string(error, "code", &msg->reply.code) &&
           get_string(error, "message", &msg->reply.message);
}

static bool decode_body(const tas_json_t *root, tas_msg_t *msg) {
    uint64_t ts;
    switch (msg->type) {
    case TAS_MSG_WELCOME: return decode_welcome(root, msg);
    case TAS_MSG_ERROR:
        return get_string(root, "code", &msg->error.code) &&
               get_string(root, "message", &msg->error.message) &&
               get_u32(root, "protocol", 0, &msg->error.protocol);
    case TAS_MSG_CMD:
        return get_u32(root, "id", 0, &msg->cmd.id) &&
               get_optional_string(root, "cap", &msg->cmd.has_cap, &msg->cmd.cap) &&
               get_string(root, "name", &msg->cmd.name) &&
               get_typed(root, "args", TAS_JSON_OBJECT, &msg->cmd.args);
    case TAS_MSG_REPLY: return decode_reply(root, msg);
    case TAS_MSG_EVENT:
        if (!get_uint(root, "ts", 0, UINT64_MAX, &ts)) return false;
        msg->event.ts = ts;
        return get_u32(root, "seq", 0, &msg->event.seq) &&
               get_string(root, "cap", &msg->event.cap) &&
               get_string(root, "name", &msg->event.name) &&
               get_typed(root, "data", TAS_JSON_OBJECT, &msg->event.data);
    case TAS_MSG_PING: return get_uint(root, "t", 0, UINT64_MAX, &msg->ping.t);
    case TAS_MSG_PONG:
        return get_uint(root, "t", 0, UINT64_MAX, &msg->pong.t) &&
               get_uint(root, "ts", 0, UINT64_MAX, &msg->pong.ts);
    default: return true;
    }
}

static const char *const TYPE_NAMES[] = {
    [TAS_MSG_HELLO] = "hello", [TAS_MSG_WELCOME] = "welcome", [TAS_MSG_PENDING] = "pending",
    [TAS_MSG_ERROR] = "error", [TAS_MSG_CMD] = "cmd",         [TAS_MSG_REPLY] = "reply",
    [TAS_MSG_EVENT] = "event", [TAS_MSG_PING] = "ping",       [TAS_MSG_PONG] = "pong",
};

tas_wire_err_t tas_msg_decode(const uint8_t *body, size_t len, tas_arena_t *arena,
                              tas_msg_t *msg) {
    *msg = (tas_msg_t){.type = TAS_MSG_NONE};
    tas_json_t *root;
    switch (tas_json_parse(body, len, arena, &root)) {
    case TAS_JSON_OK: break;
    case TAS_JSON_INVALID: return TAS_WIRE_BAD_JSON;
    case TAS_JSON_NO_MEMORY: return TAS_WIRE_NO_MEMORY;
    }
    tas_str_t type;
    if (!get_string(root, "type", &type)) return TAS_WIRE_BAD_MESSAGE;
    for (size_t t = TAS_MSG_HELLO; t <= TAS_MSG_PONG; t++) {
        if (tas_str_eq(type, TYPE_NAMES[t])) msg->type = (tas_msg_type_t)t;
    }
    if (msg->type == TAS_MSG_HELLO) return decode_hello(root, arena, msg);
    if (!decode_body(root, msg)) return TAS_WIRE_BAD_MESSAGE;
    return TAS_WIRE_OK;
}

static void write_manifest(tas_jw_t *w, const tas_manifest_t *m) {
    tas_jw_key(w, "protocol", true);
    tas_jw_uint(w, m->protocol);
    tas_jw_key(w, "device_id", false);
    tas_jw_string(w, m->device_id);
    tas_jw_key(w, "board", false);
    tas_jw_string(w, m->board);
    tas_jw_key(w, "firmware", false);
    tas_jw_string(w, m->firmware);
    tas_jw_key(w, "capabilities", false);
    tas_jw_raw(w, "[");
    for (size_t i = 0; i < m->cap_count; i++) {
        const tas_wire_cap_t *cap = &m->caps[i];
        tas_jw_raw(w, i ? ",{" : "{");
        tas_jw_key(w, "id", true);
        tas_jw_string(w, cap->id);
        tas_jw_key(w, "kind", false);
        tas_jw_string(w, cap->kind);
        if (cap->params && cap->params->items.count) {
            tas_jw_key(w, "params", false);
            tas_jw_value(w, cap->params);
        }
        tas_jw_raw(w, "}");
    }
    tas_jw_raw(w, "]");
    tas_jw_key(w, "exclusive", false);
    if (m->exclusive) tas_jw_value(w, m->exclusive);
    else tas_jw_raw(w, "[]");
}

static void write_body(tas_jw_t *w, const tas_msg_t *msg) {
    switch (msg->type) {
    case TAS_MSG_HELLO:
        tas_jw_key(w, "token", false);
        if (msg->hello.has_token) tas_jw_string(w, msg->hello.token);
        else tas_jw_raw(w, "null");
        tas_jw_key(w, "udp_port", false);
        tas_jw_uint(w, msg->hello.udp_port);
        tas_jw_key(w, "manifest", false);
        tas_jw_raw(w, "{");
        write_manifest(w, &msg->hello.manifest);
        tas_jw_raw(w, "}");
        break;
    case TAS_MSG_WELCOME:
        tas_jw_key(w, "name", false);
        tas_jw_string(w, msg->welcome.name);
        tas_jw_key(w, "conn_id", false);
        tas_jw_uint(w, msg->welcome.conn_id);
        tas_jw_key(w, "udp_port", false);
        tas_jw_uint(w, msg->welcome.udp_port);
        if (msg->welcome.has_token) {
            tas_jw_key(w, "token", false);
            tas_jw_string(w, msg->welcome.token);
        }
        break;
    case TAS_MSG_ERROR:
        tas_jw_key(w, "code", false);
        tas_jw_string(w, msg->error.code);
        tas_jw_key(w, "message", false);
        tas_jw_string(w, msg->error.message);
        tas_jw_key(w, "protocol", false);
        tas_jw_uint(w, msg->error.protocol);
        break;
    case TAS_MSG_CMD:
        tas_jw_key(w, "id", false);
        tas_jw_uint(w, msg->cmd.id);
        if (msg->cmd.has_cap) {
            tas_jw_key(w, "cap", false);
            tas_jw_string(w, msg->cmd.cap);
        }
        tas_jw_key(w, "name", false);
        tas_jw_string(w, msg->cmd.name);
        tas_jw_key(w, "args", false);
        tas_jw_value(w, msg->cmd.args);
        break;
    case TAS_MSG_REPLY:
        tas_jw_key(w, "id", false);
        tas_jw_uint(w, msg->reply.id);
        tas_jw_key(w, "ok", false);
        if (msg->reply.ok) {
            tas_jw_raw(w, "true");
            tas_jw_key(w, "result", false);
            tas_jw_value(w, msg->reply.result);
        } else {
            tas_jw_raw(w, "false");
            tas_jw_key(w, "error", false);
            tas_jw_raw(w, "{");
            tas_jw_key(w, "code", true);
            tas_jw_string(w, msg->reply.code);
            tas_jw_key(w, "message", false);
            tas_jw_string(w, msg->reply.message);
            tas_jw_raw(w, "}");
        }
        break;
    case TAS_MSG_EVENT:
        tas_jw_key(w, "seq", false);
        tas_jw_uint(w, msg->event.seq);
        tas_jw_key(w, "cap", false);
        tas_jw_string(w, msg->event.cap);
        tas_jw_key(w, "name", false);
        tas_jw_string(w, msg->event.name);
        tas_jw_key(w, "ts", false);
        tas_jw_uint(w, msg->event.ts);
        tas_jw_key(w, "data", false);
        tas_jw_value(w, msg->event.data);
        break;
    case TAS_MSG_PING:
        tas_jw_key(w, "t", false);
        tas_jw_uint(w, msg->ping.t);
        break;
    case TAS_MSG_PONG:
        tas_jw_key(w, "t", false);
        tas_jw_uint(w, msg->pong.t);
        tas_jw_key(w, "ts", false);
        tas_jw_uint(w, msg->pong.ts);
        break;
    default: break;
    }
}

int tas_msg_encode(const tas_msg_t *msg, uint8_t *buf, size_t cap) {
    if (msg->type < TAS_MSG_HELLO || msg->type > TAS_MSG_PONG) return -1;
    tas_jw_t w = {.buf = buf, .cap = cap};
    tas_jw_raw(&w, "{");
    tas_jw_key(&w, "type", true);
    tas_jw_raw(&w, "\"");
    tas_jw_raw(&w, TYPE_NAMES[msg->type]);
    tas_jw_raw(&w, "\"");
    write_body(&w, msg);
    tas_jw_raw(&w, "}");
    return w.overflow ? -1 : (int)w.len;
}

int tas_msg_frame(const tas_msg_t *msg, uint8_t *buf, size_t cap) {
    if (cap < TAS_FRAME_HEADER) return -1;
    int len = tas_msg_encode(msg, buf + TAS_FRAME_HEADER, cap - TAS_FRAME_HEADER);
    if (len < 0 || len > TAS_MAX_FRAME_BODY) return -1;
    buf[0] = TAS_FRAME_JSON;
    buf[1] = 0;
    put16(buf + 2, (uint16_t)len);
    return TAS_FRAME_HEADER + len;
}

// ---- UDP datagrams

tas_wire_err_t tas_dgram_decode(const uint8_t *data, size_t len, tas_dgram_t *dgram) {
    if (len < TAS_UDP_HEADER) return TAS_WIRE_SHORT_DATAGRAM;
    if (len - TAS_UDP_HEADER > TAS_MAX_UDP_PAYLOAD) return TAS_WIRE_DATAGRAM_TOO_LARGE;
    *dgram = (tas_dgram_t){
        .conn_id = get32(data),
        .kind = data[4],
        .stream = data[5],
        .flags = get16(data + 6),
        .seq = get32(data + 8),
        .ts = get32(data + 12) | (uint64_t)get32(data + 16) << 32,
        .payload = data + TAS_UDP_HEADER,
        .len = len - TAS_UDP_HEADER,
    };
    if (dgram->flags & TAS_UDP_FLAG_ENCRYPTED) return TAS_WIRE_ENCRYPTED;
    if (dgram->kind == TAS_DGRAM_STREAM && dgram->stream == 0) return TAS_WIRE_BAD_STREAM;
    return TAS_WIRE_OK;
}

static void put_header(uint8_t *buf, const tas_dgram_t *d) {
    put32(buf, d->conn_id);
    buf[4] = d->kind;
    buf[5] = d->stream;
    put16(buf + 6, d->flags);
    put32(buf + 8, d->seq);
    put32(buf + 12, (uint32_t)d->ts);
    put32(buf + 16, (uint32_t)(d->ts >> 32));
}

int tas_dgram_encode(const tas_dgram_t *dgram, uint8_t *buf, size_t cap) {
    if (dgram->len > TAS_MAX_UDP_PAYLOAD || cap < TAS_UDP_HEADER + dgram->len) return -1;
    put_header(buf, dgram);
    memcpy(buf + TAS_UDP_HEADER, dgram->payload, dgram->len);
    return (int)(TAS_UDP_HEADER + dgram->len);
}

tas_wire_err_t tas_mirror_decode(const tas_dgram_t *dgram, tas_arena_t *arena, tas_msg_t *event) {
    tas_wire_err_t err = tas_msg_decode(dgram->payload, dgram->len, arena, event);
    if (err) return err;
    if (event->type != TAS_MSG_EVENT || event->event.seq != dgram->seq ||
        event->event.ts != dgram->ts) {
        return TAS_WIRE_BAD_MESSAGE;
    }
    return TAS_WIRE_OK;
}

int tas_mirror_encode(uint32_t conn_id, uint16_t flags, const tas_msg_t *event, uint8_t *buf,
                      size_t cap) {
    if (event->type != TAS_MSG_EVENT || cap < TAS_UDP_HEADER) return -1;
    size_t room = cap - TAS_UDP_HEADER;
    int len = tas_msg_encode(event, buf + TAS_UDP_HEADER,
                             room < TAS_MAX_UDP_PAYLOAD ? room : TAS_MAX_UDP_PAYLOAD);
    if (len < 0) return -1;
    tas_dgram_t header = {.conn_id = conn_id,
                          .kind = TAS_DGRAM_MIRROR,
                          .flags = flags,
                          .seq = event->event.seq,
                          .ts = event->event.ts};
    put_header(buf, &header);
    return TAS_UDP_HEADER + len;
}

tas_wire_err_t tas_frame_check(size_t size, uint32_t rate, uint32_t channels, uint32_t frame_ms,
                               bool final) {
    size_t sample = (size_t)channels * 2;
    size_t frame = (size_t)rate * frame_ms / 1000 * sample;
    if (size % sample) return TAS_WIRE_PARTIAL_SAMPLE;
    if (size > frame) return TAS_WIRE_OVERSIZED_FRAME;
    if (size == 0 || (size < frame && !final)) return TAS_WIRE_SHORT_FRAME;
    return TAS_WIRE_OK;
}
