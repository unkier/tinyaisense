// The exclusive groups (tas_board_t.exclusive): which capabilities stream, and who has the pins a
// group shares.

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "kind_modules.h"

#define TAG "kinds"
#define MAX_CAPS 32       // a bit each in a mask
#define QUIET_WAIT_MS 20 // the longest a speaker's cut may take to play out, with margin

static const tas_board_t *board;
// The exclusive groups, as masks of capability indices.
static uint32_t group_caps[MAX_CAPS];

// Everything below. Held for a few register writes at most, so a spinlock.
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t active;             // capabilities with a stream open
static const tas_cap_t *pins_owner; // whom the switch hook last handed the shared pins; NULL at boot

static size_t index_of(const tas_cap_t *cap) {
    return (size_t)(cap - board->caps);
}

// The capabilities that share an exclusive group with `cap`, but not `cap`.
static uint32_t peers_of(const tas_cap_t *cap) {
    uint32_t bit = 1u << index_of(cap), peers = 0;
    for (size_t g = 0; g < board->exclusive_count; g++) {
        if (group_caps[g] & bit) peers |= group_caps[g];
    }
    return peers & ~bit;
}

void tas_exclusive_init(const tas_board_t *b) {
    board = b;
    if (b->cap_count > MAX_CAPS || b->exclusive_count > MAX_CAPS) abort();
    for (size_t g = 0; g < b->exclusive_count; g++) {
        for (size_t i = 0; i < b->exclusive[g].count; i++) {
            for (size_t c = 0; c < b->cap_count; c++) {
                if (!strcmp(b->caps[c].id, b->exclusive[g].ids[i])) group_caps[g] |= 1u << c;
            }
        }
    }
}

bool tas_kinds_streaming(tas_kind_t kind) {
    taskENTER_CRITICAL(&lock);
    uint32_t now = active;
    taskEXIT_CRITICAL(&lock);
    for (size_t i = 0; i < board->cap_count; i++) {
        if (board->caps[i].kind == kind && now & 1u << i) return true;
    }
    return false;
}

bool tas_kind_acquire(const tas_cap_t *cap) {
    uint32_t peers = peers_of(cap);
    taskENTER_CRITICAL(&lock);
    bool ok = !(active & peers);
    if (ok) active |= 1u << index_of(cap);
    taskEXIT_CRITICAL(&lock);
    return ok;
}

void tas_kind_release(const tas_cap_t *cap) {
    taskENTER_CRITICAL(&lock);
    active &= ~(1u << index_of(cap));
    taskEXIT_CRITICAL(&lock);
}

void tas_kind_take_pins(const tas_cap_t *cap) {
    uint32_t peers = peers_of(cap);
    if (!peers || !board->exclusive_switch) return;
    // A peer can't start while `cap` is active, so one quiet now stays quiet.
    for (size_t i = 0; i < board->cap_count; i++) {
        const tas_cap_t *peer = &board->caps[i];
        bool (*quiet)(const tas_cap_t *) = tas_kind_module(peer->kind)->quiet;
        if (!(peers & 1u << i) || !quiet) continue;
        for (int ms = 0; !quiet(peer) && ms < QUIET_WAIT_MS; ms++) vTaskDelay(pdMS_TO_TICKS(1));
    }
    taskENTER_CRITICAL(&lock);
    bool take = active & 1u << index_of(cap) && pins_owner != cap;
    if (take) {
        board->exclusive_switch(cap);
        pins_owner = cap;
    }
    taskEXIT_CRITICAL(&lock);
    if (take) ESP_LOGD(TAG, "the shared pins to %s", cap->id);
}
