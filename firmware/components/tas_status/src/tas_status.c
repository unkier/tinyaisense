#include "tas_status.h"

#include <string.h>

static const char *const NAMES[] = {
    [TAS_STATUS_IDENTIFY] = "identify",     [TAS_STATUS_LISTENING] = "listening",
    [TAS_STATUS_CLAIM] = "claim",           [TAS_STATUS_OFFLINE] = "offline",
    [TAS_STATUS_CONNECTING] = "connecting", [TAS_STATUS_PENDING] = "pending",
    [TAS_STATUS_NO_CONFIG] = "no_config",   [TAS_STATUS_SPEAKING] = "speaking",
    [TAS_STATUS_THINKING] = "thinking",     [TAS_STATUS_NO_AGENT] = "no_agent",
    [TAS_STATUS_AGENT] = "agent",
};

tas_status_t tas_status_shown(const tas_status_layers_t *layers) {
    const bool device[] = {
        [TAS_STATUS_IDENTIFY] = layers->identify,     [TAS_STATUS_LISTENING] = layers->listening,
        [TAS_STATUS_CLAIM] = layers->claimed,         [TAS_STATUS_OFFLINE] = layers->offline,
        [TAS_STATUS_CONNECTING] = layers->connecting, [TAS_STATUS_PENDING] = layers->pending,
        [TAS_STATUS_NO_CONFIG] = layers->no_config,   [TAS_STATUS_SPEAKING] = layers->speaking,
    };
    for (size_t state = 0; state < sizeof device / sizeof *device; state++) {
        if (device[state]) return (tas_status_t)state;
    }
    return layers->hub;
}

bool tas_status_hub_layer(const char *state, size_t len, tas_status_t *layer) {
    for (tas_status_t s = TAS_STATUS_THINKING; s <= TAS_STATUS_AGENT; s++) {
        if (strlen(NAMES[s]) == len && !memcmp(NAMES[s], state, len)) {
            *layer = s;
            return true;
        }
    }
    return false;
}

const char *tas_status_name(tas_status_t status) {
    return status <= TAS_STATUS_AGENT ? NAMES[status] : "?";
}

uint8_t tas_pattern_level(tas_pattern_t pattern, uint32_t period_ms, uint64_t t_ms) {
    if (pattern == TAS_PATTERN_SOLID || !period_ms) return 255;
    uint32_t phase = (uint32_t)(t_ms % period_ms), half = period_ms / 2;
    if (pattern == TAS_PATTERN_BLINK) return phase < half ? 255 : 0;
    // A triangle, squared so the fade looks even to the eye.
    uint32_t up = phase < half ? phase : period_ms - phase;
    uint32_t linear = up >= half ? 255 : up * 255 / half;
    return (uint8_t)(linear * linear / 255);
}
