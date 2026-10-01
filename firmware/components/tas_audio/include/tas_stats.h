#pragma once
// Summaries of timing samples, for the firmware's self-reports (e.g. the speaker cut's).

#include <stddef.h>
#include <stdint.h>

// The nearest-rank percentile `pct` (0-100) of `n` samples: the smallest sample with at least pct%
// of them at or below it, so 0 is the smallest. Sorts the samples in place. 0 with no samples.
uint32_t tas_percentile(uint32_t *samples, size_t n, unsigned pct);
