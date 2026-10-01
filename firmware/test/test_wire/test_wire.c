// Every golden vector in docs/protocol/vectors/device.vectors must pass through the C codec, as
// it does through the Python one (packages/wire/tests/test_golden_vectors.py).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unity.h>

#include "tas_wire.h"

#ifndef TAS_VECTOR_FILE
#error "TAS_VECTOR_FILE must name device.vectors"
#endif

#define MAX_BYTES 8192

typedef struct {
    char name[96];
    char layer[8];
    char expect[32];
    long rate, channels, frame_ms, final; // -1 when absent
    uint8_t *data, *canon;
    size_t data_len, canon_len;
    bool has_canon;
} vector_t;

static vector_t *vectors;
static size_t vector_count;

static void append_hex(uint8_t **bytes, size_t *len, const char *hex) {
    *bytes = realloc(*bytes, *len + strlen(hex) / 2 + 1);
    TEST_ASSERT_NOT_NULL(*bytes);
    for (const char *p = hex; *p;) {
        while (*p == ' ') p++;
        if (!*p) break;
        unsigned byte;
        TEST_ASSERT_EQUAL_INT(1, sscanf(p, "%2x", &byte));
        (*bytes)[(*len)++] = (uint8_t)byte;
        p += 2;
    }
}

static void load_vectors(void) {
    FILE *file = fopen(TAS_VECTOR_FILE, "r");
    if (!file) {
        TEST_FAIL_MESSAGE("can't open " TAS_VECTOR_FILE);
        return;
    }
    char line[256];
    vector_t *v = NULL;
    while (fgets(line, sizeof line, file)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!line[0] || line[0] == '#') continue;
        char *rest = strchr(line, ' ');
        rest = rest ? rest + 1 : line + strlen(line);
        if (!strncmp(line, "vector ", 7)) {
            vector_t *more = realloc(vectors, (vector_count + 1) * sizeof *vectors);
            TEST_ASSERT_NOT_NULL(more);
            vectors = more;
            v = &vectors[vector_count++];
            *v = (vector_t){.rate = -1, .channels = -1, .frame_ms = -1, .final = -1};
            snprintf(v->name, sizeof v->name, "%s", rest);
        } else if (!strncmp(line, "layer ", 6)) {
            snprintf(v->layer, sizeof v->layer, "%s", rest);
        } else if (!strncmp(line, "expect ", 7)) {
            snprintf(v->expect, sizeof v->expect, "%s", rest);
        } else if (!strncmp(line, "param ", 6)) {
            char key[16];
            long value;
            TEST_ASSERT_EQUAL_INT(2, sscanf(rest, "%15s %ld", key, &value));
            if (!strcmp(key, "rate")) v->rate = value;
            else if (!strcmp(key, "channels")) v->channels = value;
            else if (!strcmp(key, "frame_ms")) v->frame_ms = value;
            else if (!strcmp(key, "final")) v->final = value;
            else TEST_FAIL_MESSAGE(key);
        } else if (!strncmp(line, "hex ", 4)) {
            append_hex(&v->data, &v->data_len, rest);
        } else if (!strncmp(line, "canon ", 6)) {
            append_hex(&v->canon, &v->canon_len, rest);
            v->has_canon = true;
        } else if (strcmp(line, "end") != 0) {
            TEST_FAIL_MESSAGE(line);
        }
    }
    fclose(file);
}

// ---- Decoding and re-encoding, as test_golden_vectors.py does

typedef struct {
    tas_wire_err_t err;
    size_t messages; // TCP: messages decoded. UDP: 1 for a stream frame or mirror, else 0
    uint8_t out[MAX_BYTES];
    int out_len; // the re-encoding; -1 if it failed
} result_t;

static tas_framer_t framer;

static void take(const uint8_t *data, size_t len) {
    size_t space;
    uint8_t *to = tas_framer_space(&framer, &space);
    TEST_ASSERT_TRUE(len <= space);
    memcpy(to, data, len);
    tas_framer_commit(&framer, len);
}

// Takes every complete frame, re-encoding each message into `r`. False once it fails.
static bool drain_frames(result_t *r) {
    for (;;) {
        tas_frame_t frame;
        bool got;
        r->err = tas_framer_next(&framer, &frame, &got);
        if (r->err || !got) return r->err == TAS_WIRE_OK;
        if (frame.type != TAS_FRAME_JSON) continue;
        tas_arena_t arena = {0};
        tas_msg_t msg;
        r->err = tas_msg_decode(frame.body, frame.len, &arena, &msg);
        if (!r->err && msg.type != TAS_MSG_NONE) {
            r->messages++;
            int n = r->out_len < 0 ? -1
                                   : tas_msg_frame(&msg, r->out + r->out_len,
                                                   MAX_BYTES - (size_t)r->out_len);
            r->out_len = n < 0 ? -1 : r->out_len + n;
        }
        tas_arena_free(&arena);
        if (r->err) return false;
    }
}

static void decode_tcp(const uint8_t *data, size_t len, size_t chunk, result_t *r) {
    *r = (result_t){0};
    tas_framer_reset(&framer);
    if (!chunk) chunk = len ? len : 1;
    for (size_t i = 0; i < len; i += chunk) {
        take(data + i, len - i < chunk ? len - i : chunk);
        if (!drain_frames(r)) return;
    }
    r->err = tas_framer_close(&framer);
}

static void decode_udp(const vector_t *v, const uint8_t *data, size_t len, result_t *r) {
    *r = (result_t){0};
    tas_dgram_t dgram;
    r->err = tas_dgram_decode(data, len, &dgram);
    if (r->err) return;
    if (dgram.kind == TAS_DGRAM_STREAM) {
        if (v->final >= 0) {
            r->err = tas_frame_check(dgram.len, (uint32_t)v->rate, (uint32_t)v->channels,
                                     (uint32_t)v->frame_ms, v->final != 0);
            if (r->err) return;
        }
        r->messages = 1;
        r->out_len = tas_dgram_encode(&dgram, r->out, MAX_BYTES);
    } else if (dgram.kind == TAS_DGRAM_MIRROR) {
        tas_arena_t arena = {0};
        tas_msg_t event;
        r->err = tas_mirror_decode(&dgram, &arena, &event);
        if (!r->err) {
            r->messages = 1;
            r->out_len = tas_mirror_encode(dgram.conn_id, dgram.flags, &event, r->out, MAX_BYTES);
        }
        tas_arena_free(&arena);
    }
}

static void decode(const vector_t *v, const uint8_t *data, size_t len, result_t *r) {
    if (!strcmp(v->layer, "tcp")) decode_tcp(data, len, 0, r);
    else if (!strcmp(v->layer, "udp")) decode_udp(v, data, len, r);
    else TEST_FAIL_MESSAGE(v->layer);
}

static result_t result, other;

// Each check runs every vector, then fails if any did, naming the first.
static size_t failures;
static char first_failure[160];

static void fail(const vector_t *v, const char *why) {
    if (!failures++) snprintf(first_failure, sizeof first_failure, "%s: %s", v->name, why);
}

#define ASSERT_NO_FAILURES() TEST_ASSERT_EQUAL_size_t_MESSAGE(0, failures, first_failure)

static bool reencodes_to(const result_t *r, const uint8_t *want, size_t want_len) {
    return r->out_len >= 0 && (size_t)r->out_len == want_len && !memcmp(r->out, want, want_len);
}

static void check_positive(const vector_t *v, bool from_canon) {
    const uint8_t *in = from_canon ? v->canon : v->data;
    size_t in_len = from_canon ? v->canon_len : v->data_len;
    decode(v, in, in_len, &result);
    if (result.err) return fail(v, tas_wire_err_name(result.err));
    if (!result.messages) return fail(v, "nothing decoded");
    const uint8_t *want = v->has_canon ? v->canon : v->data;
    size_t want_len = v->has_canon ? v->canon_len : v->data_len;
    if (!reencodes_to(&result, want, want_len)) fail(v, "re-encodes differently");
}

void setUp(void) {
    failures = 0;
    first_failure[0] = 0;
}

void tearDown(void) {}

static void test_positive_vectors_round_trip_byte_identically(void) {
    for (size_t i = 0; i < vector_count; i++) {
        if (!strcmp(vectors[i].expect, "ok")) check_positive(&vectors[i], false);
    }
    ASSERT_NO_FAILURES();
}

static void test_canonical_forms_round_trip_byte_identically(void) {
    for (size_t i = 0; i < vector_count; i++) {
        if (!strcmp(vectors[i].expect, "ok") && vectors[i].has_canon) {
            check_positive(&vectors[i], true);
        }
    }
    ASSERT_NO_FAILURES();
}

static void test_ignored_vectors_decode_to_nothing(void) {
    for (size_t i = 0; i < vector_count; i++) {
        const vector_t *v = &vectors[i];
        if (strcmp(v->expect, "ignore") != 0) continue;
        decode(v, v->data, v->data_len, &result);
        if (result.err) fail(v, tas_wire_err_name(result.err));
        else if (result.messages) fail(v, "decoded something");
    }
    ASSERT_NO_FAILURES();
}

static void test_negative_vectors_fail_with_their_documented_code(void) {
    size_t negatives = 0;
    for (size_t i = 0; i < vector_count; i++) {
        const vector_t *v = &vectors[i];
        if (!strcmp(v->expect, "ok") || !strcmp(v->expect, "ignore")) continue;
        negatives++;
        decode(v, v->data, v->data_len, &result);
        if (strcmp(tas_wire_err_name(result.err), v->expect) != 0) {
            fail(v, tas_wire_err_name(result.err));
        }
    }
    TEST_ASSERT_GREATER_THAN_size_t(0, negatives);
    ASSERT_NO_FAILURES();
}

static void test_tcp_decoding_does_not_depend_on_how_the_stream_is_split(void) {
    for (size_t i = 0; i < vector_count; i++) {
        const vector_t *v = &vectors[i];
        if (strcmp(v->layer, "tcp") != 0) continue;
        if (strcmp(v->expect, "ok") != 0 && strcmp(v->expect, "ignore") != 0) continue;
        decode_tcp(v->data, v->data_len, 1, &result);
        decode_tcp(v->data, v->data_len, 0, &other);
        if (result.err != other.err || result.messages != other.messages ||
            !reencodes_to(&result, other.out, other.out_len < 0 ? 0 : (size_t)other.out_len)) {
            fail(v, "byte by byte decodes differently");
        }
    }
    ASSERT_NO_FAILURES();
}

static void test_every_decode_error_has_a_negative_vector(void) {
    for (tas_wire_err_t err = TAS_WIRE_FRAME_TOO_LARGE; err <= TAS_WIRE_OVERSIZED_FRAME; err++) {
        bool found = false;
        for (size_t i = 0; i < vector_count && !found; i++) {
            found = !strcmp(vectors[i].expect, tas_wire_err_name(err));
        }
        TEST_ASSERT_TRUE_MESSAGE(found, tas_wire_err_name(err));
    }
}

int main(void) {
    UNITY_BEGIN();
    load_vectors();
    TEST_ASSERT_GREATER_THAN_size_t(100, vector_count);
    RUN_TEST(test_positive_vectors_round_trip_byte_identically);
    RUN_TEST(test_canonical_forms_round_trip_byte_identically);
    RUN_TEST(test_ignored_vectors_decode_to_nothing);
    RUN_TEST(test_negative_vectors_fail_with_their_documented_code);
    RUN_TEST(test_tcp_decoding_does_not_depend_on_how_the_stream_is_split);
    RUN_TEST(test_every_decode_error_has_a_negative_vector);
    for (size_t i = 0; i < vector_count; i++) {
        free(vectors[i].data);
        free(vectors[i].canon);
    }
    free(vectors);
    return UNITY_END();
}
