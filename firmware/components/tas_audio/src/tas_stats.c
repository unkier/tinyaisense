#include "tas_stats.h"

#include <stdlib.h>

static int ascending(const void *a, const void *b) {
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

uint32_t tas_percentile(uint32_t *samples, size_t n, unsigned pct) {
    if (!n) return 0;
    qsort(samples, n, sizeof *samples, ascending);
    size_t rank = (n * pct + 99) / 100;
    return samples[rank ? rank - 1 : 0];
}
