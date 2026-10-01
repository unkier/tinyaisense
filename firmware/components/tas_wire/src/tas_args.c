#include "tas_wire.h"

tas_arg_t tas_arg_int(const tas_json_t *args, const char *key, int64_t min, int64_t max,
                      int64_t *out) {
    const tas_json_t *value = tas_json_get(args, key);
    if (!value) return TAS_ARG_ABSENT;
    if (value->type != TAS_JSON_INT || value->integer < min || value->integer > max) {
        return TAS_ARG_BAD;
    }
    *out = value->integer;
    return TAS_ARG_OK;
}

tas_arg_t tas_arg_enum(const tas_json_t *args, const char *key, const char *const *names,
                       size_t count, size_t *out) {
    const tas_json_t *value = tas_json_get(args, key);
    if (!value) return TAS_ARG_ABSENT;
    if (value->type != TAS_JSON_STRING) return TAS_ARG_BAD;
    for (size_t i = 0; i < count; i++) {
        if (tas_str_eq(value->text, names[i])) {
            *out = i;
            return TAS_ARG_OK;
        }
    }
    return TAS_ARG_BAD;
}

tas_arg_t tas_arg_format(const tas_json_t *args, const char *key, uint32_t rate,
                         const uint32_t *frame_ms, size_t count, uint32_t *out) {
    const tas_json_t *format = tas_json_get(args, key);
    if (!format) return TAS_ARG_ABSENT;
    if (format->type != TAS_JSON_OBJECT) return TAS_ARG_BAD;
    static const char *const CODECS[] = {"pcm_s16le"};
    size_t codec;
    int64_t got_rate, channels, ms;
    if (tas_arg_enum(format, "codec", CODECS, 1, &codec) != TAS_ARG_OK ||
        tas_arg_int(format, "rate", rate, rate, &got_rate) != TAS_ARG_OK ||
        tas_arg_int(format, "channels", 1, 1, &channels) != TAS_ARG_OK ||
        tas_arg_int(format, "frame_ms", 1, UINT32_MAX, &ms) != TAS_ARG_OK) {
        return TAS_ARG_BAD;
    }
    for (size_t i = 0; i < count; i++) {
        if (frame_ms[i] == ms) {
            *out = frame_ms[i];
            return TAS_ARG_OK;
        }
    }
    return TAS_ARG_BAD;
}

void tas_reply_fail(tas_msg_t *reply, const char *code, const char *message) {
    reply->reply.ok = false;
    reply->reply.code = tas_str(code);
    reply->reply.message = tas_str(message);
}
