#pragma once
// The reconnect backoff of docs/protocol/device.md, "Connection end and reconnect". Pure C.
//
// Every Connection that ends, or fails to open, since the last welcome is one failure in a row.
// Each waits the next step, 0.5 -> 1 -> 2 -> 5 s (the cap), plus up to 20% random jitter. After
// 3 failures in a row, and after every 3 more, mDNS is resolved again before the next attempt.

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t failures; // in a row, since the last welcome
} tas_backoff_t;

typedef struct {
    uint32_t delay_ms; // to wait before the next attempt
    bool resolve;      // resolve the hub over mDNS again first
} tas_retry_t;

// A Connection ended, or never got going. `random` is a uniform 32-bit random number: the jitter.
tas_retry_t tas_backoff_failed(tas_backoff_t *backoff, uint32_t random);
void tas_backoff_reset(tas_backoff_t *backoff);
