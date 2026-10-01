#include "tas_micq.h"

#include <string.h>

bool tas_micq_start(tas_micq_t *q, const tas_micq_config_t *cfg) {
    q->next_seq = q->count = q->dropped = 0;
    if (!cfg->frame_ms) return false;
    q->frame_samples = cfg->rate * cfg->frame_ms / 1000;
    q->slots = cfg->capacity_ms / cfg->frame_ms;
    return q->frame_samples && q->slots && q->slots <= TAS_MICQ_MAX_SLOTS &&
           q->slots * q->frame_samples <= TAS_MICQ_MAX_SAMPLES;
}

void tas_micq_push(tas_micq_t *q, const int16_t *pcm, uint64_t ts) {
    if (q->count == q->slots) {
        q->count--;
        q->dropped++;
    }
    uint32_t slot = q->next_seq % q->slots;
    memcpy(q->pcm + slot * q->frame_samples, pcm, q->frame_samples * sizeof *pcm);
    q->ts[slot] = ts;
    q->next_seq++;
    q->count++;
}

bool tas_micq_pop(tas_micq_t *q, tas_micq_frame_t *frame) {
    if (!q->count) return false;
    uint32_t seq = q->next_seq - q->count--, slot = seq % q->slots;
    *frame = (tas_micq_frame_t){.seq = seq,
                                .ts = q->ts[slot],
                                .pcm = q->pcm + slot * q->frame_samples,
                                .samples = q->frame_samples};
    return true;
}

bool tas_micq_last_seq(const tas_micq_t *q, uint32_t *seq) {
    if (!q->next_seq) return false;
    *seq = q->next_seq - 1;
    return true;
}
