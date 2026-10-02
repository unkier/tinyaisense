#pragma once
// A button's edges, sans-IO (docs/protocol/kinds/button.md): it acts on the first edge once a
// re-read of the pin ~1 ms later confirms it, then ignores bounces for 20 ms. The edge's `ts` is
// the first edge. A re-read that doesn't confirm was a glitch, e.g. GPIO39's on the ESP32 when
// the ADC powers up, and is ignored. Pure C, no IDF calls.

#include <stdbool.h>
#include <stdint.h>

#define TAS_DEBOUNCE_CONFIRM_US 1000
#define TAS_DEBOUNCE_LOCKOUT_US 20000
#define TAS_DEBOUNCE_NONE UINT64_MAX // no deadline

typedef enum {
    TAS_DEBOUNCE_IDLE,
    TAS_DEBOUNCE_CONFIRMING, // an edge came: the re-read is due
    TAS_DEBOUNCE_LOCKED,     // an edge was reported: bounces are ignored
} tas_debounce_phase_t;

typedef struct {
    bool down; // the state last reported
    tas_debounce_phase_t phase;
    uint64_t first_us;    // the edge being confirmed
    uint64_t last_us;     // the last edge seen in the lockout; 0 for none
    uint64_t deadline_us; // when the re-read is due, or the lockout ends
} tas_debounce_t;

// A button edge, confirmed.
typedef struct {
    bool down;   // it went down; up otherwise
    uint64_t ts; // device us of its first edge
} tas_edge_t;

// A button whose pin reads `down` now.
void tas_debounce_init(tas_debounce_t *d, bool down);
// The pin changed at device time `at_us`: what the button's ISR says.
void tas_debounce_edge(tas_debounce_t *d, uint64_t at_us);
// When to call tas_debounce_check next, or TAS_DEBOUNCE_NONE until the next edge.
uint64_t tas_debounce_deadline(const tas_debounce_t *d);
// At the deadline, when the pin reads `down`: true with the edge it confirms in `edge`. Once the
// lockout ends, a pin that reads other than the state last reported changed during it: that
// change is reported, timed by the last edge seen in the lockout.
bool tas_debounce_check(tas_debounce_t *d, uint64_t now_us, bool down, tas_edge_t *edge);
