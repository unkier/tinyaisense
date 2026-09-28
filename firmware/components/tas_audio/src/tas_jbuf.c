#include "tas_jbuf.h"

#include <string.h>

#include "tas_ramp.h"

bool tas_jbuf_start(tas_jbuf_t *jb, const tas_jbuf_config_t *cfg) {
    memset(jb, 0, sizeof *jb);
    if (!cfg->frame_ms) return false;
    jb->needs_ramp_in = true;
    jb->rate = cfg->rate;
    jb->frame_ms = cfg->frame_ms;
    jb->frame_samples = cfg->rate * cfg->frame_ms / 1000;
    jb->slots = cfg->capacity_ms / cfg->frame_ms;
    // At least the first frame, and never more than the buffer holds.
    jb->prefill_frames = (cfg->prefill_ms + cfg->frame_ms - 1) / cfg->frame_ms;
    if (jb->prefill_frames < 1) jb->prefill_frames = 1;
    if (jb->prefill_frames > jb->slots) jb->prefill_frames = jb->slots;
    jb->ramp = cfg->rate * cfg->ramp_ms / 1000;
    return jb->frame_samples && jb->slots && jb->slots <= TAS_JBUF_MAX_SLOTS &&
           jb->slots * jb->frame_samples <= TAS_JBUF_MAX_SAMPLES && jb->ramp &&
           jb->ramp <= jb->frame_samples;
}

// Where frame `seq` sits in pcm.
static size_t slot_at(const tas_jbuf_t *jb, uint32_t seq) {
    return (seq % jb->slots) * jb->frame_samples;
}

// The samples held for frame `seq`, or 0.
static uint32_t held(const tas_jbuf_t *jb, uint32_t seq) {
    uint32_t slot = seq % jb->slots;
    return jb->seq[slot] == seq ? jb->len[slot] : 0;
}

// How many frames are held at or after `from`, and the oldest of them.
static uint32_t held_from(const tas_jbuf_t *jb, uint32_t from, uint32_t *oldest) {
    uint32_t n = 0;
    for (uint32_t slot = 0; slot < jb->slots; slot++) {
        if (!jb->len[slot] || jb->seq[slot] < from) continue;
        if (!n++ || jb->seq[slot] < *oldest) *oldest = jb->seq[slot];
    }
    return n;
}

static void advance(tas_jbuf_t *jb, uint64_t samples) {
    jb->position += samples;
    jb->stats.played_ms = (uint32_t)(jb->position * 1000 / jb->rate);
}

// Moves the playhead to the oldest frame held at or after `from`, if any. The frames it passes
// are lost.
static void skip_to_oldest_from(tas_jbuf_t *jb, uint32_t from) {
    uint32_t oldest;
    if (!held_from(jb, from, &oldest) || oldest == jb->head) return;
    uint32_t skipped = oldest - jb->head;
    jb->head = oldest;
    jb->needs_ramp_in = true;
    jb->stats.lost += skipped;
    advance(jb, (uint64_t)skipped * jb->frame_samples);
}

bool tas_jbuf_push(tas_jbuf_t *jb, uint32_t seq, const int16_t *pcm, size_t samples) {
    if (!samples || samples > jb->frame_samples) return false;
    if (seq < jb->head || held(jb, seq) || (jb->draining && seq > jb->last_seq)) return false;
    bool overflow = seq - jb->head >= jb->slots;
    uint32_t slot = seq % jb->slots;
    memcpy(jb->pcm + slot_at(jb, seq), pcm, samples * sizeof *pcm);
    jb->seq[slot] = seq;
    jb->len[slot] = (uint16_t)samples;
    // Drop what the buffer no longer spans, then anything missing up to the oldest frame left.
    if (overflow) skip_to_oldest_from(jb, seq - jb->slots + 1);
    return true;
}

void tas_jbuf_drain(tas_jbuf_t *jb, uint32_t last_seq) {
    jb->draining = true;
    jb->last_seq = last_seq;
}

// A frame is whole when it's full length, or the drain's short final frame.
static bool whole(const tas_jbuf_t *jb, uint32_t seq) {
    uint32_t len = held(jb, seq);
    return len == jb->frame_samples || (len && jb->draining && seq == jb->last_seq);
}

// Silence for a frame period. If the last frame didn't end ramped (an overflow skipped past its
// successor), its final sample is held and ramped down first.
static tas_jbuf_out_t silence(tas_jbuf_t *jb, int16_t *out, tas_jbuf_out_t outcome) {
    if (jb->last_out) {
        for (uint32_t i = 0; i < jb->ramp; i++) out[i] = jb->last_out;
        tas_ramp_out(out, jb->ramp, 1, jb->ramp);
        jb->last_out = 0;
    }
    jb->needs_ramp_in = true;
    return outcome;
}

static tas_jbuf_out_t play(tas_jbuf_t *jb, int16_t *out, uint32_t seq) {
    uint32_t len = held(jb, seq);
    memcpy(out, jb->pcm + slot_at(jb, seq), len * sizeof *out);
    if (jb->needs_ramp_in) tas_ramp_in(out, len, 1, jb->ramp, &jb->last_out);
    bool last = jb->draining && seq == jb->last_seq;
    // Look one frame ahead: a frame with no successor ends on a ramp down.
    jb->needs_ramp_in = last || !whole(jb, seq + 1);
    if (jb->needs_ramp_in) {
        uint32_t tail = len < jb->ramp ? len : jb->ramp;
        tas_ramp_out(out + len - tail, tail, 1, tail);
    }
    jb->last_out = out[len - 1];
    advance(jb, len);
    return TAS_JBUF_PLAY;
}

tas_jbuf_out_t tas_jbuf_pull(tas_jbuf_t *jb, int16_t *out) {
    memset(out, 0, jb->frame_samples * sizeof *out);
    uint32_t oldest;
    if (!jb->started) {
        if (!jb->draining && held_from(jb, jb->head, &oldest) < jb->prefill_frames) {
            return silence(jb, out, TAS_JBUF_PREFILL);
        }
        jb->started = true;
    }
    // Resuming from a pause: the pause already stood in for any frames still missing.
    if (jb->paused) skip_to_oldest_from(jb, jb->head);
    if (jb->draining && jb->head > jb->last_seq) return silence(jb, out, TAS_JBUF_DONE);

    uint32_t seq = jb->head;
    bool can_skip = jb->draining || held_from(jb, seq + 1, &oldest); // past a missing frame
    if (held(jb, seq) && !whole(jb, seq) && can_skip) {
        // A short frame is legal only as the final one, and this one can't be.
        jb->len[seq % jb->slots] = 0;
        jb->stats.short_frames++;
    }
    if (whole(jb, seq) || can_skip) {
        jb->paused = false;
        jb->head++;
        if (whole(jb, seq)) return play(jb, out, seq);
        jb->stats.lost++;
        advance(jb, jb->frame_samples);
        return silence(jb, out, TAS_JBUF_LOST);
    }
    // An empty buffer, or a short frame that may yet turn out to be the final one.
    if (!jb->paused) jb->stats.underruns++;
    jb->paused = true;
    jb->stats.underrun_ms += jb->frame_ms;
    return silence(jb, out, TAS_JBUF_UNDERRUN);
}

tas_jbuf_stats_t tas_jbuf_stats(const tas_jbuf_t *jb) {
    return jb->stats;
}
