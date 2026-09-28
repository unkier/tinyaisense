#include "tas_cut.h"

#include <string.h>

#include "tas_ramp.h"

bool tas_cut_init(tas_cut_t *cut, size_t descs, size_t frames, size_t channels, size_t ramp) {
    memset(cut, 0, sizeof *cut);
    cut->descs = descs;
    cut->frames = frames;
    cut->channels = channels;
    cut->ramp = ramp;
    return descs >= 2 && descs <= TAS_CUT_MAX_DESCS && ramp && ramp <= frames;
}

void tas_cut_sent(tas_cut_t *cut, void *dma_buf) {
    for (size_t i = 0; i < cut->known; i++) {
        if (cut->ring[i] == dma_buf) {
            cut->last = i;
            return;
        }
    }
    if (cut->known < cut->descs) {
        cut->ring[cut->known] = dma_buf;
        cut->last = cut->known++;
    }
}

bool tas_cut_patch(tas_cut_t *cut) {
    if (cut->known < cut->descs) return false;
    size_t playing = (cut->last + 1) % cut->descs;
    size_t next = (playing + 1) % cut->descs;
    // The head first: the DMA reaches it within one descriptor.
    tas_ramp_out(cut->ring[next], cut->frames, cut->channels, cut->ramp);
    for (size_t i = (next + 1) % cut->descs; i != playing; i = (i + 1) % cut->descs) {
        memset(cut->ring[i], 0, cut->frames * cut->channels * sizeof *cut->ring[i]);
    }
    return true;
}
