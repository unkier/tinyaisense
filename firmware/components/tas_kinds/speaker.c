// The speaker Kind (docs/protocol/kinds/speaker.md). The jitter buffer, ramps, low-cut, volume, drain/drained, played_ms and the cut.

#include <stdatomic.h>

#include "esp_log.h"
#include "kind_modules.h"
#include "sdkconfig.h"

static tas_kind_instances_t instances;
// Set by the hub, never stored: each boot starts at the Kconfig default.
static atomic_uchar volumes[TAS_KINDS_MAX_INSTANCES];

static void init(const tas_cap_t *cap) {
    atomic_store(&volumes[tas_kind_add(&instances, cap)], CONFIG_TAS_SPEAKER_VOLUME);
    ESP_LOGI("speaker", "%s ready", cap->id);
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)cap;
    return tas_kind_audio_params(board->spk_cond.rate, arena);
}

static void command(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply) {
    (void)arena;
    if (tas_str_eq(cmd->cmd.name, "set_volume")) {
        int64_t level;
        if (tas_arg_int(cmd->cmd.args, "level", 0, 100, &level) != TAS_ARG_OK) {
            return tas_reply_fail(reply, "invalid_args", "level must be an integer from 0 to 100");
        }
        atomic_store(&volumes[tas_kind_find(&instances, cap)], (unsigned char)level);
        return;
    }
    // start, stop and drain come with the speaker's streams.
    tas_reply_fail(reply, "unsupported", "speaker has no such command yet");
}

uint8_t tas_kinds_volume(const tas_cap_t *cap) {
    return atomic_load(&volumes[tas_kind_find(&instances, cap)]);
}

const tas_kind_module_t tas_speaker_module = {
    .name = "speaker", .init = init, .params = params, .command = command};
