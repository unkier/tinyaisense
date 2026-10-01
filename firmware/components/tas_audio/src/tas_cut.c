#include "tas_cut.h"

#include <string.h>

#include "tas_ramp.h"

typedef struct {
    size_t known, last;
    uint32_t sents;
    int64_t sent_us[TAS_CUT_MAX_DESCS];
} snapshot_t;

static void write_begin(tas_cut_t *cut) {
    atomic_store_explicit(&cut->seq, atomic_load_explicit(&cut->seq, memory_order_relaxed) + 1,
                          memory_order_relaxed);
    atomic_thread_fence(memory_order_release);
}

static void write_end(tas_cut_t *cut) {
    atomic_fetch_add_explicit(&cut->seq, 1, memory_order_release);
}

// What on_sent last wrote.
static snapshot_t read_snapshot(const tas_cut_t *cut) {
    snapshot_t snap;
    uint32_t seq;
    do {
        seq = atomic_load_explicit(&cut->seq, memory_order_acquire);
        snap.known = cut->known;
        snap.last = cut->last;
        snap.sents = cut->sents;
        for (size_t i = 0; i < cut->cfg.descs; i++) snap.sent_us[i] = cut->sent_us[i];
        atomic_thread_fence(memory_order_acquire);
    } while ((seq & 1) || seq != atomic_load_explicit(&cut->seq, memory_order_relaxed));
    return snap;
}

bool tas_cut_init(tas_cut_t *cut, const tas_cut_config_t *cfg) {
    memset(cut, 0, sizeof *cut);
    cut->cfg = *cfg;
    return cfg->descs >= 2 && cfg->descs <= TAS_CUT_MAX_DESCS && cfg->frames && cfg->channels &&
           cfg->rate && cfg->ramp && cfg->fetch_lead + cfg->ramp <= cfg->frames;
}

bool tas_cut_sent(tas_cut_t *cut, void *dma_buf, int64_t now_us) {
    size_t descs = cut->cfg.descs, known = cut->known, last = cut->last, desc = 0;
    while (desc < known && cut->ring[desc] != dma_buf) desc++;
    if (desc == descs) return false; // not the ring's: can't happen
    // The descriptors finished since the last on_sent: more than one if the ISR ran late.
    uint32_t done = known == descs ? (uint32_t)((desc + descs - last - 1) % descs + 1) : 1;
    uint32_t sents = cut->sents;
    write_begin(cut);
    if (desc == known) {
        cut->ring[desc] = dma_buf;
        cut->known = known + 1;
    }
    for (uint32_t i = 1; i <= done; i++) cut->sent_us[(last + i) % descs] = now_us; // at the latest
    cut->last = desc;
    cut->sents = sents + done;
    write_end(cut);
    return atomic_load(&cut->cutting) && atomic_load(&cut->reach) - sents - 1 < done;
}

void tas_cut_restart(tas_cut_t *cut, int64_t now_us) {
    atomic_store(&cut->cutting, false);
    write_begin(cut);
    cut->last = cut->cfg.descs - 1; // so the first descriptor is the one playing
    cut->sent_us[cut->last] = now_us;
    write_end(cut);
}

bool tas_cut_patch(tas_cut_t *cut, int64_t now_us) {
    const tas_cut_config_t *cfg = &cut->cfg;
    snapshot_t snap = read_snapshot(cut);
    if (snap.known < cfg->descs) return false;

    // Frames from the start of the descriptor playing that the DMA may have, rounded up.
    int64_t sent_us = snap.sent_us[snap.last];
    int64_t elapsed = now_us > sent_us ? now_us - sent_us : 0;
    int64_t fetched = (elapsed * cfg->rate + 999999) / 1000000 + (int64_t)cfg->fetch_lead;
    // The first descriptor after the one playing with room for the ramp after what's fetched.
    int64_t frames = (int64_t)cfg->frames, latest = frames - (int64_t)cfg->ramp;
    int64_t ahead = fetched > latest ? (fetched - latest + frames - 1) / frames : 1;
    if (ahead >= (int64_t)cfg->descs) return false;
    int64_t start = ahead * frames;
    size_t offset = fetched > start ? (size_t)(fetched - start) : 0;

    size_t playing = (snap.last + 1) % cfg->descs;
    size_t ramped = (playing + (size_t)ahead) % cfg->descs;
    // The ramp first: the DMA reaches it soonest.
    tas_ramp_out(cut->ring[ramped] + offset * cfg->channels, cfg->frames - offset, cfg->channels,
                 cfg->ramp);
    for (size_t i = (ramped + 1) % cfg->descs; i != playing; i = (i + 1) % cfg->descs) {
        memset(cut->ring[i], 0, cfg->frames * cfg->channels * sizeof *cut->ring[i]);
    }
    cut->ramped = ramped;
    cut->offset = offset;
    atomic_store(&cut->reach, snap.sents + (uint32_t)ahead);
    atomic_store(&cut->cutting, true);
    return true;
}

bool tas_cut_silent_at(const tas_cut_t *cut, int64_t *at_us) {
    if (!atomic_load(&cut->cutting)) return false;
    const tas_cut_config_t *cfg = &cut->cfg;
    snapshot_t snap = read_snapshot(cut);
    if (snap.sents - atomic_load(&cut->reach) >= cfg->descs) return false;
    // The DMA reached the ramp as the descriptor before it finished.
    int64_t reached_us = snap.sent_us[(cut->ramped + cfg->descs - 1) % cfg->descs];
    *at_us = reached_us + (int64_t)(cut->offset + cfg->ramp) * 1000000 / cfg->rate;
    return true;
}
