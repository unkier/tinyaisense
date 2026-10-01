#include "tas_json.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

// ---- Arena

#define BLOCK_SIZE 1024
#define ALIGN sizeof(max_align_t)

struct tas_arena_block {
    tas_arena_block_t *next;
    size_t size; // usable bytes
    max_align_t data[];
};

void *tas_arena_alloc(tas_arena_t *arena, size_t size) {
    size = size ? (size + ALIGN - 1) / ALIGN * ALIGN : ALIGN;
    tas_arena_block_t *block = arena->blocks;
    if (!block || block->size - arena->used < size) {
        size_t cap = size > BLOCK_SIZE ? size : BLOCK_SIZE;
        if (arena->limit && arena->total + cap > arena->limit) return NULL;
        block = malloc(sizeof *block + cap);
        if (!block) return NULL;
        block->next = arena->blocks;
        block->size = cap;
        arena->blocks = block;
        arena->used = 0;
        arena->total += cap;
    }
    void *p = (unsigned char *)block->data + arena->used;
    arena->used += size;
    return p;
}

void tas_arena_free(tas_arena_t *arena) {
    while (arena->blocks) {
        tas_arena_block_t *next = arena->blocks->next;
        free(arena->blocks);
        arena->blocks = next;
    }
    arena->used = arena->total = 0;
}

// ---- Strings

tas_str_t tas_str(const char *s) {
    return (tas_str_t){s, strlen(s)};
}

bool tas_str_eq(tas_str_t a, const char *b) {
    return tas_str_same(a, tas_str(b));
}

bool tas_str_same(tas_str_t a, tas_str_t b) {
    return a.len == b.len && !memcmp(a.ptr, b.ptr, a.len);
}

static char *copy(tas_arena_t *arena, const void *data, size_t len) {
    char *p = tas_arena_alloc(arena, len + 1);
    if (p) {
        memcpy(p, data, len);
        p[len] = 0;
    }
    return p;
}

// ---- Parser

typedef struct {
    const uint8_t *p, *end;
    tas_arena_t *arena;
    tas_json_status_t status; // the first failure
} parser_t;

static void *fail(parser_t *ps, tas_json_status_t status) {
    if (ps->status == TAS_JSON_OK) ps->status = status;
    return NULL;
}

static tas_json_t *node(tas_arena_t *arena, tas_json_type_t type) {
    tas_json_t *n = tas_arena_alloc(arena, sizeof *n);
    if (n) *n = (tas_json_t){.type = type};
    return n;
}

static tas_json_t *new_node(parser_t *ps, tas_json_type_t type) {
    tas_json_t *n = node(ps->arena, type);
    return n ? n : fail(ps, TAS_JSON_NO_MEMORY);
}

static void skip_space(parser_t *ps) {
    while (ps->p < ps->end &&
           (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r')) {
        ps->p++;
    }
}

static bool at(const parser_t *ps, char c) {
    return ps->p < ps->end && *ps->p == (uint8_t)c;
}

static bool is_digit(const parser_t *ps) {
    return ps->p < ps->end && *ps->p >= '0' && *ps->p <= '9';
}

static void append(tas_json_t *parent, tas_json_t *child) {
    if (parent->items.last) parent->items.last->next = child;
    else parent->items.first = child;
    parent->items.last = child;
    parent->items.count++;
}

static int hex_digit(uint8_t c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Reads the 4 hex digits of a \u escape. -1 if they aren't.
static long hex4(const uint8_t *p, const uint8_t *end) {
    if (end - p < 4) return -1;
    long value = 0;
    for (int i = 0; i < 4; i++) {
        int digit = hex_digit(p[i]);
        if (digit < 0) return -1;
        value = value * 16 + digit;
    }
    return value;
}

static size_t put_utf8(char *out, unsigned long cp) {
    if (cp < 0x80) {
        out[0] = (char)cp;
        return 1;
    }
    if (cp < 0x800) {
        out[0] = (char)(0xc0 | cp >> 6);
        out[1] = (char)(0x80 | (cp & 0x3f));
        return 2;
    }
    if (cp < 0x10000) {
        out[0] = (char)(0xe0 | cp >> 12);
        out[1] = (char)(0x80 | (cp >> 6 & 0x3f));
        out[2] = (char)(0x80 | (cp & 0x3f));
        return 3;
    }
    out[0] = (char)(0xf0 | cp >> 18);
    out[1] = (char)(0x80 | (cp >> 12 & 0x3f));
    out[2] = (char)(0x80 | (cp >> 6 & 0x3f));
    out[3] = (char)(0x80 | (cp & 0x3f));
    return 4;
}

// The length of the well-formed UTF-8 sequence at p, or 0: no overlong forms, no surrogates,
// nothing past U+10FFFF.
static size_t utf8_length(const uint8_t *p, const uint8_t *end) {
    uint8_t c = p[0];
    size_t n;
    uint8_t lo = 0x80, hi = 0xbf; // the range of the second byte
    if (c >= 0xc2 && c <= 0xdf) n = 2;
    else if (c >= 0xe0 && c <= 0xef) n = 3;
    else if (c >= 0xf0 && c <= 0xf4) n = 4;
    else return 0;
    if (c == 0xe0) lo = 0xa0;
    else if (c == 0xed) hi = 0x9f;
    else if (c == 0xf0) lo = 0x90;
    else if (c == 0xf4) hi = 0x8f;
    if (end - p < (ptrdiff_t)n || p[1] < lo || p[1] > hi) return 0;
    for (size_t i = 2; i < n; i++) {
        if (p[i] < 0x80 || p[i] > 0xbf) return 0;
    }
    return n;
}

// Decodes the string at the opening quote into the arena.
static bool parse_string(parser_t *ps, tas_str_t *out) {
    ps->p++; // the opening quote
    const uint8_t *close = ps->p;
    while (close < ps->end && *close != '"') {
        if (*close == '\\' && ps->end - close < 2) return fail(ps, TAS_JSON_INVALID), false;
        close += *close == '\\' ? 2 : 1;
    }
    if (close >= ps->end) return fail(ps, TAS_JSON_INVALID), false;
    // Escapes only shrink: the decoded string fits in the raw length.
    char *buf = tas_arena_alloc(ps->arena, (size_t)(close - ps->p) + 1);
    if (!buf) return fail(ps, TAS_JSON_NO_MEMORY), false;
    size_t len = 0;
    const uint8_t *p = ps->p;
    while (p < close) {
        uint8_t c = *p;
        if (c < 0x20) return fail(ps, TAS_JSON_INVALID), false;
        if (c >= 0x80) {
            size_t n = utf8_length(p, close);
            if (!n) return fail(ps, TAS_JSON_INVALID), false;
            memcpy(buf + len, p, n);
            len += n;
            p += n;
            continue;
        }
        if (c != '\\') {
            buf[len++] = (char)c;
            p++;
            continue;
        }
        char e = (char)p[1];
        p += 2;
        switch (e) {
        case '"': buf[len++] = '"'; break;
        case '\\': buf[len++] = '\\'; break;
        case '/': buf[len++] = '/'; break;
        case 'b': buf[len++] = '\b'; break;
        case 'f': buf[len++] = '\f'; break;
        case 'n': buf[len++] = '\n'; break;
        case 'r': buf[len++] = '\r'; break;
        case 't': buf[len++] = '\t'; break;
        case 'u': {
            long cp = hex4(p, close);
            if (cp < 0 || (cp >= 0xdc00 && cp <= 0xdfff)) return fail(ps, TAS_JSON_INVALID), false;
            p += 4;
            if (cp >= 0xd800 && cp <= 0xdbff) {
                long low = close - p >= 6 && p[0] == '\\' && p[1] == 'u' ? hex4(p + 2, close) : -1;
                if (low < 0xdc00 || low > 0xdfff) return fail(ps, TAS_JSON_INVALID), false;
                cp = 0x10000 + ((cp - 0xd800) << 10) + (low - 0xdc00);
                p += 6;
            }
            len += put_utf8(buf + len, (unsigned long)cp);
            break;
        }
        default: return fail(ps, TAS_JSON_INVALID), false;
        }
    }
    buf[len] = 0;
    ps->p = close + 1;
    *out = (tas_str_t){buf, len};
    return true;
}

static tas_json_t *parse_number(parser_t *ps) {
    const uint8_t *start = ps->p;
    bool negative = at(ps, '-');
    if (negative) ps->p++;
    if (at(ps, '0')) ps->p++;
    else if (is_digit(ps)) {
        while (is_digit(ps)) ps->p++;
    } else {
        return fail(ps, TAS_JSON_INVALID);
    }
    const uint8_t *int_end = ps->p;
    bool integer = true;
    if (at(ps, '.')) {
        ps->p++;
        if (!is_digit(ps)) return fail(ps, TAS_JSON_INVALID);
        while (is_digit(ps)) ps->p++;
        integer = false;
    }
    if (at(ps, 'e') || at(ps, 'E')) {
        ps->p++;
        if (at(ps, '+') || at(ps, '-')) ps->p++;
        if (!is_digit(ps)) return fail(ps, TAS_JSON_INVALID);
        while (is_digit(ps)) ps->p++;
        integer = false;
    }
    if (integer) {
        const uint8_t *digits = start + negative;
        if (int_end - digits > 16) return fail(ps, TAS_JSON_INVALID);
        int64_t value = 0;
        for (const uint8_t *d = digits; d < int_end; d++) value = value * 10 + (*d - '0');
        if (value > TAS_JSON_MAX_INT) return fail(ps, TAS_JSON_INVALID);
        tas_json_t *n = new_node(ps, TAS_JSON_INT);
        if (n) n->integer = negative ? -value : value;
        return n;
    }
    char *literal = copy(ps->arena, start, (size_t)(ps->p - start));
    if (!literal) return fail(ps, TAS_JSON_NO_MEMORY);
    if (isinf(strtod(literal, NULL))) return fail(ps, TAS_JSON_INVALID);
    tas_json_t *n = new_node(ps, TAS_JSON_REAL);
    if (n) n->text = (tas_str_t){literal, (size_t)(ps->p - start)};
    return n;
}

static tas_json_t *parse_literal(parser_t *ps, const char *word, tas_json_type_t type) {
    size_t len = strlen(word);
    if ((size_t)(ps->end - ps->p) < len || memcmp(ps->p, word, len) != 0) {
        return fail(ps, TAS_JSON_INVALID);
    }
    ps->p += len;
    return new_node(ps, type);
}

static tas_json_t *parse_value(parser_t *ps, int depth);

static bool has_key(const tas_json_t *object, tas_str_t key) {
    for (const tas_json_t *m = object->items.first; m; m = m->next) {
        if (tas_str_same(m->key, key)) return true;
    }
    return false;
}

static tas_json_t *parse_object(parser_t *ps, int depth) {
    tas_json_t *object = new_node(ps, TAS_JSON_OBJECT);
    if (!object) return NULL;
    ps->p++;
    skip_space(ps);
    if (at(ps, '}')) {
        ps->p++;
        return object;
    }
    for (;;) {
        tas_str_t key;
        if (!at(ps, '"')) return fail(ps, TAS_JSON_INVALID);
        if (!parse_string(ps, &key)) return NULL;
        if (has_key(object, key)) return fail(ps, TAS_JSON_INVALID);
        skip_space(ps);
        if (!at(ps, ':')) return fail(ps, TAS_JSON_INVALID);
        ps->p++;
        skip_space(ps);
        tas_json_t *value = parse_value(ps, depth + 1);
        if (!value) return NULL;
        value->key = key;
        append(object, value);
        skip_space(ps);
        if (at(ps, '}')) {
            ps->p++;
            return object;
        }
        if (!at(ps, ',')) return fail(ps, TAS_JSON_INVALID);
        ps->p++;
        skip_space(ps);
    }
}

static tas_json_t *parse_array(parser_t *ps, int depth) {
    tas_json_t *array = new_node(ps, TAS_JSON_ARRAY);
    if (!array) return NULL;
    ps->p++;
    skip_space(ps);
    if (at(ps, ']')) {
        ps->p++;
        return array;
    }
    for (;;) {
        tas_json_t *value = parse_value(ps, depth + 1);
        if (!value) return NULL;
        append(array, value);
        skip_space(ps);
        if (at(ps, ']')) {
            ps->p++;
            return array;
        }
        if (!at(ps, ',')) return fail(ps, TAS_JSON_INVALID);
        ps->p++;
        skip_space(ps);
    }
}

// A value at nesting level `depth`, the top-level object being level 1.
static tas_json_t *parse_value(parser_t *ps, int depth) {
    if (ps->p >= ps->end) return fail(ps, TAS_JSON_INVALID);
    switch (*ps->p) {
    case '{':
    case '[':
        if (depth > TAS_JSON_MAX_DEPTH) return fail(ps, TAS_JSON_INVALID);
        return *ps->p == '{' ? parse_object(ps, depth) : parse_array(ps, depth);
    case '"': {
        tas_str_t text;
        if (!parse_string(ps, &text)) return NULL;
        tas_json_t *n = new_node(ps, TAS_JSON_STRING);
        if (n) n->text = text;
        return n;
    }
    case 't': return parse_literal(ps, "true", TAS_JSON_TRUE);
    case 'f': return parse_literal(ps, "false", TAS_JSON_FALSE);
    case 'n': return parse_literal(ps, "null", TAS_JSON_NULL);
    default: return parse_number(ps);
    }
}

tas_json_status_t tas_json_parse(const uint8_t *text, size_t len, tas_arena_t *arena,
                                 tas_json_t **out) {
    parser_t ps = {.p = text, .end = text + len, .arena = arena};
    skip_space(&ps);
    tas_json_t *root = parse_value(&ps, 1);
    if (root) {
        skip_space(&ps);
        if (root->type != TAS_JSON_OBJECT || ps.p != ps.end) fail(&ps, TAS_JSON_INVALID);
    }
    *out = ps.status == TAS_JSON_OK ? root : NULL;
    return ps.status;
}

const tas_json_t *tas_json_get(const tas_json_t *object, const char *key) {
    if (!object || object->type != TAS_JSON_OBJECT) return NULL;
    for (const tas_json_t *m = object->items.first; m; m = m->next) {
        if (tas_str_eq(m->key, key)) return m;
    }
    return NULL;
}

// ---- Building

tas_json_t *tas_json_object(tas_arena_t *arena) {
    return node(arena, TAS_JSON_OBJECT);
}

tas_json_t *tas_json_array(tas_arena_t *arena) {
    return node(arena, TAS_JSON_ARRAY);
}

tas_json_t *tas_json_int(tas_arena_t *arena, int64_t value) {
    tas_json_t *n = node(arena, TAS_JSON_INT);
    if (n) n->integer = value;
    return n;
}

tas_json_t *tas_json_bool(tas_arena_t *arena, bool value) {
    return node(arena, value ? TAS_JSON_TRUE : TAS_JSON_FALSE);
}

tas_json_t *tas_json_string(tas_arena_t *arena, const char *value) {
    size_t len = strlen(value);
    char *text = copy(arena, value, len);
    tas_json_t *n = text ? node(arena, TAS_JSON_STRING) : NULL;
    if (n) n->text = (tas_str_t){text, len};
    return n;
}

tas_json_t *tas_json_push(tas_json_t *array, tas_json_t *value) {
    if (!array || !value) return NULL;
    append(array, value);
    return array;
}

tas_json_t *tas_json_put(tas_arena_t *arena, tas_json_t *object, const char *key,
                         tas_json_t *value) {
    if (!object || !value) return NULL;
    size_t len = strlen(key);
    char *k = copy(arena, key, len);
    if (!k) return NULL;
    value->key = (tas_str_t){k, len};
    append(object, value);
    return object;
}

// ---- Writer

static void put(tas_jw_t *w, const void *data, size_t len) {
    if (w->overflow) return;
    if (w->cap - w->len < len) {
        w->overflow = true;
        return;
    }
    memcpy(w->buf + w->len, data, len);
    w->len += len;
}

void tas_jw_raw(tas_jw_t *w, const char *text) {
    put(w, text, strlen(text));
}

void tas_jw_string(tas_jw_t *w, tas_str_t s) {
    put(w, "\"", 1);
    const uint8_t *p = (const uint8_t *)s.ptr, *end = p + s.len;
    while (p < end) {
        // Copy the run that needs no escape in one go.
        const uint8_t *run = p;
        while (p < end && *p >= 0x20 && *p != '"' && *p != '\\') p++;
        put(w, run, (size_t)(p - run));
        if (p == end) break;
        char esc[7] = {'\\', 0};
        switch (*p) {
        case '"': esc[1] = '"'; break;
        case '\\': esc[1] = '\\'; break;
        case '\b': esc[1] = 'b'; break;
        case '\f': esc[1] = 'f'; break;
        case '\n': esc[1] = 'n'; break;
        case '\r': esc[1] = 'r'; break;
        case '\t': esc[1] = 't'; break;
        default: {
            static const char hex[] = "0123456789abcdef";
            memcpy(esc + 1, "u00", 3);
            esc[4] = hex[*p >> 4];
            esc[5] = hex[*p & 0xf];
        }
        }
        tas_jw_raw(w, esc);
        p++;
    }
    put(w, "\"", 1);
}

void tas_jw_uint(tas_jw_t *w, uint64_t value) {
    char digits[20];
    size_t n = 0;
    do {
        digits[sizeof digits - ++n] = (char)('0' + value % 10);
        value /= 10;
    } while (value);
    put(w, digits + sizeof digits - n, n);
}

void tas_jw_int(tas_jw_t *w, int64_t value) {
    if (value < 0) {
        put(w, "-", 1);
        tas_jw_uint(w, (uint64_t)0 - (uint64_t)value);
    } else {
        tas_jw_uint(w, (uint64_t)value);
    }
}

void tas_jw_key(tas_jw_t *w, const char *key, bool first) {
    if (!first) put(w, ",", 1);
    tas_jw_string(w, tas_str(key));
    put(w, ":", 1);
}

void tas_jw_value(tas_jw_t *w, const tas_json_t *value) {
    switch (value->type) {
    case TAS_JSON_NULL: tas_jw_raw(w, "null"); break;
    case TAS_JSON_FALSE: tas_jw_raw(w, "false"); break;
    case TAS_JSON_TRUE: tas_jw_raw(w, "true"); break;
    case TAS_JSON_INT: tas_jw_int(w, value->integer); break;
    case TAS_JSON_REAL: put(w, value->text.ptr, value->text.len); break;
    case TAS_JSON_STRING: tas_jw_string(w, value->text); break;
    case TAS_JSON_ARRAY:
    case TAS_JSON_OBJECT: {
        bool object = value->type == TAS_JSON_OBJECT;
        put(w, object ? "{" : "[", 1);
        for (const tas_json_t *item = value->items.first; item; item = item->next) {
            if (item != value->items.first) put(w, ",", 1);
            if (object) {
                tas_jw_string(w, item->key);
                put(w, ":", 1);
            }
            tas_jw_value(w, item);
        }
        put(w, object ? "}" : "]", 1);
        break;
    }
    }
}
