#include "tas_debounce.h"

void tas_debounce_init(tas_debounce_t *d, bool down) {
    *d = (tas_debounce_t){.down = down, .deadline_us = TAS_DEBOUNCE_NONE};
}

void tas_debounce_edge(tas_debounce_t *d, uint64_t at_us) {
    switch (d->phase) {
    case TAS_DEBOUNCE_IDLE:
        d->phase = TAS_DEBOUNCE_CONFIRMING;
        d->first_us = at_us;
        d->deadline_us = at_us + TAS_DEBOUNCE_CONFIRM_US;
        break;
    case TAS_DEBOUNCE_CONFIRMING: break; // the first edge times it
    case TAS_DEBOUNCE_LOCKED: d->last_us = at_us; break;
    }
}

uint64_t tas_debounce_deadline(const tas_debounce_t *d) {
    return d->deadline_us;
}

bool tas_debounce_check(tas_debounce_t *d, uint64_t now_us, bool down, tas_edge_t *edge) {
    if (down == d->down) {
        d->phase = TAS_DEBOUNCE_IDLE;
        d->deadline_us = TAS_DEBOUNCE_NONE;
        return false;
    }
    uint64_t ts = d->first_us;
    if (d->phase == TAS_DEBOUNCE_LOCKED) ts = d->last_us ? d->last_us : now_us;
    d->down = down;
    *edge = (tas_edge_t){.down = down, .ts = ts};
    d->phase = TAS_DEBOUNCE_LOCKED;
    d->last_us = 0;
    d->deadline_us = now_us + TAS_DEBOUNCE_LOCKOUT_US;
    return true;
}
