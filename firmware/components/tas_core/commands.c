// Commands from the hub. They run in the ctrl task, so none may block.

#include "core.h"

static void fail(tas_msg_t *reply, const char *code, const char *message) {
    reply->reply.ok = false;
    reply->reply.code = tas_str(code);
    reply->reply.message = tas_str(message);
}

// `status{state}`: sets the hub layer of the Status.
static void status(const tas_json_t *args, tas_msg_t *reply) {
    const tas_json_t *state = tas_json_get(args, "state");
    tas_status_t layer;
    if (!state || state->type != TAS_JSON_STRING ||
        !tas_status_hub_layer(state->text.ptr, state->text.len, &layer)) {
        return fail(reply, "invalid_args", "state must be no_agent, agent or thinking");
    }
    status_set_hub(layer);
}

void commands_handle(const tas_msg_t *cmd, tas_arena_t *arena, tas_msg_t *reply) {
    (void)arena;
    if (!cmd->cmd.has_cap && tas_str_eq(cmd->cmd.name, "status")) {
        return status(cmd->cmd.args, reply);
    }
    fail(reply, "unsupported", "unknown command");
}
