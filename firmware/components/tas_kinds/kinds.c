#include <stdlib.h>

#include "freertos/FreeRTOS.h"
#include "kind_modules.h"
#include "tas_streams.h"

const tas_kinds_core_t *tas_kinds_core;
const uint32_t tas_kind_frame_ms[TAS_KIND_FRAME_MS_COUNT] = {10, 20};

static const tas_board_t *board;

// The stream ids closed lately. A spinlock: a few bytes copied at most.
static portMUX_TYPE closed_lock = portMUX_INITIALIZER_UNLOCKED;
static tas_closed_t closed;

const tas_kind_module_t *tas_kind_module(tas_kind_t kind) {
    switch (kind) {
    case TAS_KIND_MIC: return &tas_mic_module;
    case TAS_KIND_SPEAKER: return &tas_speaker_module;
    case TAS_KIND_BUTTON: return &tas_button_module;
    case TAS_KIND_LED: return &tas_led_module;
    }
    return NULL;
}

void tas_kinds_init(const tas_board_t *b, const tas_kinds_core_t *core) {
    board = b;
    tas_kinds_core = core;
    tas_exclusive_init(b);
    tas_closed_clear(&closed);
    for (size_t i = 0; i < b->cap_count; i++) {
        tas_kind_module(b->caps[i].kind)->init(b, &b->caps[i]);
    }
}

// Tells the buttons, or the other capabilities, that the Connection ended.
static void disconnect(bool buttons) {
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        const tas_kind_module_t *module = tas_kind_module(cap->kind);
        if ((cap->kind == TAS_KIND_BUTTON) == buttons && module->disconnected) {
            module->disconnected(cap);
        }
    }
}

void tas_kinds_disconnected(void) {
    // The buttons first: once a press being handled is done, the Reflex is disarmed, so what that
    // press opened is ended with the rest.
    disconnect(true);
    disconnect(false);
    taskENTER_CRITICAL(&closed_lock);
    tas_closed_clear(&closed);
    taskEXIT_CRITICAL(&closed_lock);
}

void tas_kind_mark_closed(uint8_t stream) {
    taskENTER_CRITICAL(&closed_lock);
    tas_closed_add(&closed, stream);
    taskEXIT_CRITICAL(&closed_lock);
}

tas_frame_result_t tas_kinds_frame_in(const tas_dgram_t *frame, tas_wire_err_t *err) {
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        const tas_kind_module_t *module = tas_kind_module(cap->kind);
        if (!module->frame_in) continue;
        tas_frame_result_t result = module->frame_in(cap, frame, err);
        if (result != TAS_FRAME_UNKNOWN) return result;
    }
    // A stream is marked closed as it closes, so a frame no stream took finds its id here.
    taskENTER_CRITICAL(&closed_lock);
    bool closed_lately = tas_closed_has(&closed, frame->stream);
    taskEXIT_CRITICAL(&closed_lock);
    return closed_lately ? TAS_FRAME_CLOSED : TAS_FRAME_UNKNOWN;
}

bool tas_kinds_frame_out(tas_mic_frame_t *frame) {
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *cap = &board->caps[i];
        const tas_kind_module_t *module = tas_kind_module(cap->kind);
        if (module->frame_out && module->frame_out(cap, frame)) return true;
    }
    return false;
}

// A pcm_s16le mono format at `rate`, with `frame_ms`: a number, or the array of them.
static tas_json_t *format_json(uint32_t rate, tas_json_t *frame_ms, tas_arena_t *arena) {
    tas_json_t *format = tas_json_object(arena);
    format = tas_json_put(arena, format, "codec", tas_json_string(arena, "pcm_s16le"));
    format = tas_json_put(arena, format, "rate", tas_json_int(arena, rate));
    format = tas_json_put(arena, format, "channels", tas_json_int(arena, 1));
    return tas_json_put(arena, format, "frame_ms", frame_ms);
}

tas_json_t *tas_kind_audio_params(uint32_t rate, tas_arena_t *arena) {
    tas_json_t *frame_ms = tas_json_array(arena);
    for (size_t i = 0; i < TAS_KIND_FRAME_MS_COUNT; i++) {
        frame_ms = tas_json_push(frame_ms, tas_json_int(arena, tas_kind_frame_ms[i]));
    }
    tas_json_t *formats = tas_json_push(tas_json_array(arena), format_json(rate, frame_ms, arena));
    return tas_json_put(arena, tas_json_object(arena), "formats", formats);
}

bool tas_kind_stream_args(const tas_json_t *args, uint32_t rate, uint8_t *stream,
                          uint32_t *frame_ms, tas_msg_t *reply) {
    int64_t id;
    if (tas_arg_int(args, "stream", TAS_STREAM_HUB_FIRST, TAS_STREAM_HUB_LAST, &id) !=
        TAS_ARG_OK) {
        tas_reply_fail(reply, "invalid_args", "stream must be a hub-range id, 1 to 127");
        return false;
    }
    if (tas_arg_format(args, "format", rate, tas_kind_frame_ms, TAS_KIND_FRAME_MS_COUNT,
                       frame_ms) != TAS_ARG_OK) {
        tas_reply_fail(reply, "invalid_args", "the format isn't advertised");
        return false;
    }
    *stream = (uint8_t)id;
    return true;
}

void tas_kind_format_reply(uint32_t rate, uint32_t frame_ms, tas_arena_t *arena, tas_msg_t *reply) {
    tas_json_t *format = format_json(rate, tas_json_int(arena, frame_ms), arena);
    tas_json_t *result = tas_json_put(arena, tas_json_object(arena), "format", format);
    if (!result) return tas_reply_fail(reply, "failed", "out of memory");
    reply->reply.result = result;
}

size_t tas_kind_add(tas_kind_instances_t *instances, const tas_cap_t *cap) {
    if (instances->count == TAS_KINDS_MAX_INSTANCES) abort();
    instances->caps[instances->count] = cap;
    return instances->count++;
}

size_t tas_kind_find(const tas_kind_instances_t *instances, const tas_cap_t *cap) {
    for (size_t i = 0; i < instances->count; i++) {
        if (instances->caps[i] == cap) return i;
    }
    abort();
}
