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

void tas_reply_fail(tas_msg_t *reply, const char *code, const char *message) {
    reply->reply.ok = false;
    reply->reply.code = tas_str(code);
    reply->reply.message = tas_str(message);
}
