#include "tas_reflex.h"

#include <string.h>

#define DATA_MAX 128 // bytes of an event's data
// The speakers a press may cut: those named, then the start mic's exclusive peers.
#define TARGETS_MAX (2 * TAS_REFLEX_MAX_STOP)

static const char REFLEX[] = "reflex";

static const tas_cap_t *find_cap(const tas_board_t *board, tas_str_t id) {
    for (size_t i = 0; i < board->cap_count; i++) {
        if (tas_str_eq(id, board->caps[i].id)) return &board->caps[i];
    }
    return NULL;
}

void tas_reflex_init(tas_reflex_t *r, const tas_reflex_config_t *cfg) {
    *r = (tas_reflex_t){.cfg = *cfg, .last_stream = TAS_REFLEX_STREAM_LAST};
}

// The parsed args of an armed set_reflex.
typedef struct {
    const tas_cap_t *stop[TAS_REFLEX_MAX_STOP];
    size_t stop_count;
    const tas_cap_t *start;
    uint32_t frame_ms;
} arming_t;

// Whether `value` is an array of strings, or absent.
static bool string_array(const tas_json_t *value) {
    if (!value) return true;
    if (value->type != TAS_JSON_ARRAY) return false;
    for (const tas_json_t *item = value->items.first; item; item = item->next) {
        if (item->type != TAS_JSON_STRING) return false;
    }
    return true;
}

// Whether `value` is an audio format by the contract. Whether the mic advertises it is checked once
// it's known whose.
static bool format_object(const tas_json_t *value) {
    if (!value || value->type != TAS_JSON_OBJECT) return false;
    const tas_json_t *codec = tas_json_get(value, "codec");
    int64_t n;
    return codec && codec->type == TAS_JSON_STRING &&
           tas_arg_int(value, "rate", 1, INT64_MAX, &n) == TAS_ARG_OK &&
           tas_arg_int(value, "channels", 1, INT64_MAX, &n) == TAS_ARG_OK &&
           tas_arg_int(value, "frame_ms", 1, INT64_MAX, &n) == TAS_ARG_OK;
}

// Whether `value` is a `{cap, format}` object, or absent.
static bool start_object(const tas_json_t *value) {
    if (!value) return true;
    if (value->type != TAS_JSON_OBJECT) return false;
    const tas_json_t *cap = tas_json_get(value, "cap");
    return cap && cap->type == TAS_JSON_STRING && format_object(tas_json_get(value, "format"));
}

// The targets of an armed set_reflex. A message, a literal, if they don't fit the board.
static const char *parse_arming(const tas_reflex_t *r, const tas_json_t *stop,
                                const tas_json_t *start, arming_t *out) {
    const tas_board_t *board = r->cfg.board;
    *out = (arming_t){0};
    if (stop) {
        if (stop->items.count > TAS_REFLEX_MAX_STOP) return "stop names too many speakers";
        for (const tas_json_t *id = stop->items.first; id; id = id->next) {
            const tas_cap_t *cap = find_cap(board, id->text);
            if (!cap || cap->kind != TAS_KIND_SPEAKER) return "stop names a capability that isn't a speaker";
            out->stop[out->stop_count++] = cap;
        }
    }
    if (start) {
        const tas_cap_t *mic = find_cap(board, tas_json_get(start, "cap")->text);
        if (!mic || mic->kind != TAS_KIND_MIC) return "start names a capability that isn't a mic";
        if (tas_arg_format(start, "format", board->mic_cond.rate, r->cfg.frame_ms,
                           r->cfg.frame_ms_count, &out->frame_ms) != TAS_ARG_OK) {
            return "the mic doesn't advertise that format";
        }
        out->start = mic;
    }
    return NULL;
}

void tas_reflex_set(tas_reflex_t *r, const tas_json_t *args, tas_msg_t *reply) {
    const tas_json_t *armed = tas_json_get(args, "armed");
    const tas_json_t *stop = tas_json_get(args, "stop");
    const tas_json_t *start = tas_json_get(args, "start");
    if (!armed || (armed->type != TAS_JSON_TRUE && armed->type != TAS_JSON_FALSE)) {
        return tas_reply_fail(reply, "invalid_args", "armed must be true or false");
    }
    if (!string_array(stop)) {
        return tas_reply_fail(reply, "invalid_args", "stop must be an array of capability ids");
    }
    if (!start_object(start)) {
        return tas_reply_fail(reply, "invalid_args", "start must be {cap, format}");
    }
    if (armed->type == TAS_JSON_FALSE) {
        r->armed = false;
        return;
    }
    arming_t arming;
    const char *refused = parse_arming(r, stop, start, &arming);
    if (refused) return tas_reply_fail(reply, "invalid_args", refused);
    r->armed = true;
    memcpy(r->stop, arming.stop, sizeof r->stop);
    r->stop_count = arming.stop_count;
    r->start = arming.start;
    r->start_frame_ms = arming.frame_ms;
}

// Whether a mic has stream `stream` open.
static bool stream_open(const tas_reflex_t *r, uint8_t stream) {
    const tas_board_t *board = r->cfg.board;
    const tas_reflex_ops_t *ops = &r->cfg.ops;
    for (size_t i = 0; i < board->cap_count; i++) {
        uint8_t open;
        if (board->caps[i].kind == TAS_KIND_MIC &&
            ops->mic_stream(ops->ctx, &board->caps[i], &open) && open == stream) {
            return true;
        }
    }
    return false;
}

// The next device-range stream id, round-robin, past any a mic has open.
static uint8_t next_stream(tas_reflex_t *r) {
    do {
        r->last_stream = r->last_stream == TAS_REFLEX_STREAM_LAST ? TAS_REFLEX_STREAM_FIRST
                                                                  : r->last_stream + 1;
    } while (stream_open(r, r->last_stream));
    return r->last_stream;
}

// Whether `cap` shares an exclusive group with `peer`.
static bool exclusive_peers(const tas_board_t *board, const tas_cap_t *cap, const tas_cap_t *peer) {
    for (size_t g = 0; g < board->exclusive_count; g++) {
        bool has_cap = false, has_peer = false;
        for (size_t i = 0; i < board->exclusive[g].count; i++) {
            has_cap = has_cap || !strcmp(board->exclusive[g].ids[i], cap->id);
            has_peer = has_peer || !strcmp(board->exclusive[g].ids[i], peer->id);
        }
        if (has_cap && has_peer) return true;
    }
    return false;
}

// The speakers a press cuts: the stop targets, then the start mic's exclusive peers. How many.
static size_t targets(const tas_reflex_t *r, const tas_cap_t **out) {
    const tas_board_t *board = r->cfg.board;
    size_t n = 0;
    for (size_t i = 0; i < r->stop_count; i++) out[n++] = r->stop[i];
    if (!r->start) return n;
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        if (cap->kind != TAS_KIND_SPEAKER || !exclusive_peers(board, r->start, cap)) continue;
        bool named = false;
        for (size_t j = 0; j < r->stop_count; j++) named = named || r->stop[j] == cap;
        if (!named && n < TARGETS_MAX) out[n++] = cap;
    }
    return n;
}

static bool emit(const tas_reflex_t *r, const tas_cap_t *cap, const char *name, uint64_t ts,
                 const tas_jw_t *w, bool mirror) {
    return r->cfg.ops.emit(r->cfg.ops.ctx, cap->id, name, ts, w->buf, w->len, mirror);
}

// A speaker the Reflex cut.
typedef struct {
    const tas_cap_t *cap;
    uint8_t stream;
    tas_jbuf_stats_t heard;
} cut_t;

static void emit_stopped(const tas_reflex_t *r, const cut_t *cut, uint64_t now_us) {
    uint8_t data[DATA_MAX];
    tas_jw_t w = {.buf = data, .cap = sizeof data};
    tas_jw_raw(&w, "{");
    tas_jw_key(&w, "stream", true);
    tas_jw_uint(&w, cut->stream);
    tas_jw_key(&w, "reason", false);
    tas_jw_string(&w, tas_str(REFLEX));
    tas_jw_key(&w, "played_ms", false);
    tas_jw_uint(&w, cut->heard.played_ms);
    tas_jw_key(&w, "lost", false);
    tas_jw_uint(&w, cut->heard.lost);
    tas_jw_key(&w, "underruns", false);
    tas_jw_uint(&w, cut->heard.underruns);
    tas_jw_key(&w, "underrun_ms", false);
    tas_jw_uint(&w, cut->heard.underrun_ms);
    tas_jw_raw(&w, "}");
    emit(r, cut->cap, "stopped", now_us, &w, false);
}

static void emit_started(const tas_reflex_t *r, uint64_t now_us) {
    uint8_t data[DATA_MAX];
    tas_jw_t w = {.buf = data, .cap = sizeof data};
    tas_jw_raw(&w, "{");
    tas_jw_key(&w, "stream", true);
    tas_jw_uint(&w, r->opened_stream);
    tas_jw_key(&w, "format", false);
    tas_jw_raw(&w, "{");
    tas_jw_key(&w, "codec", true);
    tas_jw_string(&w, tas_str("pcm_s16le"));
    tas_jw_key(&w, "rate", false);
    tas_jw_uint(&w, r->cfg.board->mic_cond.rate);
    tas_jw_key(&w, "channels", false);
    tas_jw_uint(&w, 1);
    tas_jw_key(&w, "frame_ms", false);
    tas_jw_uint(&w, r->start_frame_ms);
    tas_jw_raw(&w, "}}");
    emit(r, r->opened_mic, "started", now_us, &w, false);
}

static void down(tas_reflex_t *r, uint64_t ts, uint64_t now_us) {
    const tas_reflex_ops_t *ops = &r->cfg.ops;
    // A mic streaming already is the hub's to handle: the press is only an edge.
    uint8_t open;
    bool act = r->armed && !(r->start && ops->mic_stream(ops->ctx, r->start, &open));
    // The cuts come first, before anything goes out: they're what the human hears.
    const tas_cap_t *speakers[TARGETS_MAX] = {0};
    size_t speaker_count = act ? targets(r, speakers) : 0;
    cut_t cuts[TARGETS_MAX];
    size_t cut_count = 0;
    for (size_t i = 0; i < speaker_count; i++) {
        cut_t *cut = &cuts[cut_count];
        cut->cap = speakers[i];
        if (ops->cut(ops->ctx, cut->cap, ts, &cut->stream, &cut->heard)) cut_count++;
    }
    uint8_t empty[] = "{}";
    tas_jw_t w = {.buf = empty, .cap = 2, .len = 2};
    if (!emit(r, r->cfg.button, "down", ts, &w, true)) return;
    r->pressed = true;
    for (size_t i = 0; i < cut_count; i++) emit_stopped(r, &cuts[i], now_us);
    if (!act || !r->start) return;
    uint8_t stream = next_stream(r);
    if (!ops->mic_open(ops->ctx, r->start, stream, r->start_frame_ms)) return;
    r->opened_mic = r->start;
    r->opened_stream = stream;
    emit_started(r, now_us);
}

static void emit_mic_stopped(const tas_reflex_t *r, const tas_mic_end_t *end, uint64_t now_us) {
    uint8_t data[DATA_MAX];
    tas_jw_t w = {.buf = data, .cap = sizeof data};
    tas_jw_raw(&w, "{");
    tas_jw_key(&w, "stream", true);
    tas_jw_uint(&w, r->opened_stream);
    tas_jw_key(&w, "reason", false);
    tas_jw_string(&w, tas_str(REFLEX));
    tas_jw_key(&w, "last_seq", false);
    if (end->any) tas_jw_uint(&w, end->last_seq);
    else tas_jw_raw(&w, "null");
    tas_jw_key(&w, "dropped", false);
    tas_jw_uint(&w, end->dropped);
    tas_jw_raw(&w, "}");
    emit(r, r->opened_mic, "stopped", now_us, &w, false);
}

static void up(tas_reflex_t *r, uint64_t ts, uint64_t now_us) {
    const tas_reflex_ops_t *ops = &r->cfg.ops;
    if (!r->pressed) return;
    r->pressed = false;
    uint8_t empty[] = "{}";
    tas_jw_t w = {.buf = empty, .cap = 2, .len = 2};
    emit(r, r->cfg.button, "up", ts, &w, true);
    if (!r->opened_mic) return;
    tas_mic_end_t end;
    if (ops->mic_close(ops->ctx, r->opened_mic, r->opened_stream, &end)) {
        emit_mic_stopped(r, &end, now_us);
    }
    r->opened_mic = NULL;
}

void tas_reflex_edge(tas_reflex_t *r, const tas_edge_t *edge, uint64_t now_us) {
    if (edge->down) down(r, edge->ts, now_us);
    else up(r, edge->ts, now_us);
}

void tas_reflex_disconnected(tas_reflex_t *r) {
    r->armed = false;
    r->pressed = false;
    r->opened_mic = NULL;
}
