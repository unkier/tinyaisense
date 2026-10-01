#pragma once
// Strict JSON for the control protocol (docs/protocol/device.md, "JSON rules"): a parser that
// rejects everything the rules reject, into a tree held in an arena, and a writer of canonical
// JSON. Pure C, no IDF calls.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TAS_JSON_MAX_DEPTH 16
#define TAS_JSON_MAX_INT 9007199254740991LL // 2^53 - 1

// Memory for parsed trees and built messages: blocks from malloc, freed all at once.
typedef struct tas_arena_block tas_arena_block_t;
typedef struct {
    tas_arena_block_t *blocks; // the newest first
    size_t used;               // bytes used in the newest block
    size_t total;              // bytes allocated across blocks
    size_t limit;              // the most it may allocate; 0 means no limit
} tas_arena_t;

// NULL when the limit is reached or malloc fails. Aligned for any type.
void *tas_arena_alloc(tas_arena_t *arena, size_t size);
// Frees every block. The arena can be used again.
void tas_arena_free(tas_arena_t *arena);

// A UTF-8 string with its length; it may hold U+0000. There's a NUL after the last byte.
typedef struct {
    const char *ptr;
    size_t len;
} tas_str_t;

tas_str_t tas_str(const char *s);
bool tas_str_eq(tas_str_t a, const char *b);
bool tas_str_same(tas_str_t a, tas_str_t b);

typedef enum {
    TAS_JSON_NULL,
    TAS_JSON_FALSE,
    TAS_JSON_TRUE,
    TAS_JSON_INT,  // a number with no fraction or exponent
    TAS_JSON_REAL, // any other number; only its literal is kept
    TAS_JSON_STRING,
    TAS_JSON_ARRAY,
    TAS_JSON_OBJECT,
} tas_json_type_t;

typedef struct tas_json tas_json_t;
struct tas_json {
    tas_json_type_t type;
    tas_str_t key;    // the member's key, in an object
    tas_json_t *next; // the next element or member
    union {
        int64_t integer; // TAS_JSON_INT, within +-(2^53 - 1)
        tas_str_t text;  // TAS_JSON_STRING: the decoded string. TAS_JSON_REAL: the literal
        struct {
            tas_json_t *first, *last;
            size_t count;
        } items; // TAS_JSON_ARRAY and TAS_JSON_OBJECT, in order
    };
};

typedef enum {
    TAS_JSON_OK,
    TAS_JSON_INVALID,   // breaks the JSON rules: bad_json
    TAS_JSON_NO_MEMORY, // the arena ran out
} tas_json_status_t;

// Parses one JSON object, the whole of `text`. The tree lives in `arena` and holds no pointer
// into `text`.
tas_json_status_t tas_json_parse(const uint8_t *text, size_t len, tas_arena_t *arena,
                                 tas_json_t **out);

// The member `key` of `object`, or NULL.
const tas_json_t *tas_json_get(const tas_json_t *object, const char *key);

// Building a tree. Each returns NULL when the arena runs out, and takes a NULL argument as such,
// so a chain of calls needs one check at the end. Strings are copied.
tas_json_t *tas_json_object(tas_arena_t *arena);
tas_json_t *tas_json_array(tas_arena_t *arena);
tas_json_t *tas_json_int(tas_arena_t *arena, int64_t value);
tas_json_t *tas_json_bool(tas_arena_t *arena, bool value);
tas_json_t *tas_json_string(tas_arena_t *arena, const char *value);
// Appends `value` to `array`. Returns `array`, or NULL if either is NULL.
tas_json_t *tas_json_push(tas_json_t *array, tas_json_t *value);
// Appends the member `key` to `object`; the caller keeps keys unique. Returns `object`, or NULL.
tas_json_t *tas_json_put(tas_arena_t *arena, tas_json_t *object, const char *key,
                         tas_json_t *value);

// Canonical JSON into a fixed buffer. Writing past `cap` sets `overflow` and writes nothing more.
typedef struct {
    uint8_t *buf;
    size_t cap, len;
    bool overflow;
} tas_jw_t;

void tas_jw_raw(tas_jw_t *w, const char *text);
void tas_jw_string(tas_jw_t *w, tas_str_t s);
void tas_jw_int(tas_jw_t *w, int64_t value);
void tas_jw_uint(tas_jw_t *w, uint64_t value);
// `"key":`, after a comma unless `first`.
void tas_jw_key(tas_jw_t *w, const char *key, bool first);
void tas_jw_value(tas_jw_t *w, const tas_json_t *value);
