#include "tas_playout.h"

#include <string.h>

// Whether sent count `at` has been reached, across the wrap.
static bool reached(const tas_playout_t *p, uint32_t at) {
    return (int32_t)(p->sents - at) >= 0;
}

bool tas_playout_init(tas_playout_t *p, size_t descs) {
    memset(p, 0, sizeof *p);
    p->descs = descs;
    return descs >= 2 && descs <= TAS_PLAYOUT_MAX_DESCS;
}

void tas_playout_restart(tas_playout_t *p) {
    p->freed_count = 0;
    p->written_done_at = p->sents;
}

void tas_playout_sent(tas_playout_t *p, uint32_t sents) {
    p->sents = sents;
    if (p->freed_count == p->descs - 1) {
        p->freed_first = (p->freed_first + 1) % p->descs;
        p->freed_count--;
    }
    p->freed[(p->freed_first + p->freed_count++) % p->descs] = sents;
}

void tas_playout_wrote(tas_playout_t *p) {
    // A write needs a free descriptor; with none known, the latest it could play.
    uint32_t freed = p->sents;
    if (p->freed_count) {
        freed = p->freed[p->freed_first];
        p->freed_first = (p->freed_first + 1) % p->descs;
        p->freed_count--;
    }
    p->written_done_at = freed + (uint32_t)p->descs;
}

void tas_playout_frame(tas_playout_t *p, const tas_jbuf_stats_t *stats) {
    if (p->frames_count == TAS_PLAYOUT_MAX_FRAMES) {
        p->heard = p->frames[p->frames_first].stats;
        p->frames_first = (p->frames_first + 1) % TAS_PLAYOUT_MAX_FRAMES;
        p->frames_count--;
    }
    size_t i = (p->frames_first + p->frames_count++) % TAS_PLAYOUT_MAX_FRAMES;
    p->frames[i] = (tas_playout_frame_t){.done_at = p->written_done_at, .stats = *stats};
}

void tas_playout_stream(tas_playout_t *p) {
    p->frames_count = 0;
    p->written_done_at = p->sents;
    p->heard = (tas_jbuf_stats_t){0};
}

tas_jbuf_stats_t tas_playout_heard(tas_playout_t *p) {
    while (p->frames_count && reached(p, p->frames[p->frames_first].done_at)) {
        p->heard = p->frames[p->frames_first].stats;
        p->frames_first = (p->frames_first + 1) % TAS_PLAYOUT_MAX_FRAMES;
        p->frames_count--;
    }
    return p->heard;
}

bool tas_playout_idle(const tas_playout_t *p) {
    return reached(p, p->written_done_at);
}
