#include "tas_backoff.h"

static const uint32_t STEPS_MS[] = {500, 1000, 2000, 5000};
#define STEP_COUNT (sizeof STEPS_MS / sizeof *STEPS_MS)
#define JITTER_DIVISOR 5 // up to a fifth of the step
#define RESOLVE_EVERY 3

tas_retry_t tas_backoff_failed(tas_backoff_t *backoff, uint32_t random) {
    uint32_t step = STEPS_MS[backoff->failures < STEP_COUNT ? backoff->failures : STEP_COUNT - 1];
    backoff->failures++;
    uint32_t jitter = (uint32_t)((uint64_t)step * random / JITTER_DIVISOR >> 32);
    return (tas_retry_t){step + jitter, backoff->failures % RESOLVE_EVERY == 0};
}

void tas_backoff_reset(tas_backoff_t *backoff) {
    backoff->failures = 0;
}
