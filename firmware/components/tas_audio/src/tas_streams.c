#include "tas_streams.h"

void tas_closed_clear(tas_closed_t *closed) {
    closed->count = closed->next = 0;
}

void tas_closed_add(tas_closed_t *closed, uint8_t id) {
    closed->ids[closed->next] = id;
    closed->next = (closed->next + 1) % TAS_STREAMS_CLOSED;
    if (closed->count < TAS_STREAMS_CLOSED) closed->count++;
}

bool tas_closed_has(const tas_closed_t *closed, uint8_t id) {
    for (size_t i = 0; i < closed->count; i++) {
        if (closed->ids[i] == id) return true;
    }
    return false;
}
