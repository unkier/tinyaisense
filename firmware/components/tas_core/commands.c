// Commands from the hub. They run in the ctrl task, so none may block. Device-level commands are
// answered here; a capability's go to its Kind module.

#include "core.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "tas_kinds.h"

// The firmware's tasks, for diag's stack_hwm.
static const char *const TASKS[] = {"ctrl", "udp_rx", "udp_tx", "reflex",
                                    "spk", "mic", "led", "console"};

static const tas_board_t *board;

// `status{state}`: sets the hub layer of the Status.
static void status(const tas_json_t *args, tas_msg_t *reply) {
    const tas_json_t *state = tas_json_get(args, "state");
    tas_status_t layer;
    if (!state || state->type != TAS_JSON_STRING ||
        !tas_status_hub_layer(state->text.ptr, state->text.len, &layer)) {
        return tas_reply_fail(reply, "invalid_args", "state must be no_agent, agent or thinking");
    }
    status_set_hub(layer);
}

// `identify{s?}`: the white blink, for `s` seconds from now.
static void identify(const tas_json_t *args, tas_msg_t *reply) {
    int64_t s = 5;
    if (tas_arg_int(args, "s", 1, 300, &s) == TAS_ARG_BAD) {
        return tas_reply_fail(reply, "invalid_args", "s must be an integer from 1 to 300");
    }
    status_identify((uint32_t)s);
}

static const char *reset_reason(void) {
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "ext";
    case ESP_RST_SW: return "sw";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    case ESP_RST_EFUSE: return "efuse";
    case ESP_RST_PWR_GLITCH: return "pwr_glitch";
    case ESP_RST_CPU_LOCKUP: return "cpu_lockup";
    default: return "unknown";
    }
}

// `diag{}`: the device's health.
static void diag(tas_arena_t *a, tas_msg_t *reply) {
    tas_json_t *stacks = tas_json_object(a);
    for (size_t i = 0; i < sizeof TASKS / sizeof *TASKS; i++) {
        TaskHandle_t task = xTaskGetHandle(TASKS[i]);
        if (!task) continue;
        // In bytes: the IDF's stacks count bytes, not words.
        tas_json_t *bytes = tas_json_int(a, uxTaskGetStackHighWaterMark(task));
        stacks = tas_json_put(a, stacks, TASKS[i], bytes);
    }
    tas_json_t *r = tas_json_object(a);
    r = tas_json_put(a, r, "uptime_s", tas_json_int(a, esp_timer_get_time() / 1000000));
    r = tas_json_put(a, r, "reset_reason", tas_json_string(a, reset_reason()));
    r = tas_json_put(a, r, "heap_free", tas_json_int(a, esp_get_free_heap_size()));
    r = tas_json_put(a, r, "heap_min", tas_json_int(a, esp_get_minimum_free_heap_size()));
    r = tas_json_put(a, r, "rssi", tas_json_int(a, net_rssi()));
    r = tas_json_put(a, r, "reconnects", tas_json_int(a, ctrl_connections_lost()));
    r = tas_json_put(a, r, "stack_hwm", stacks);
    if (!r) return tas_reply_fail(reply, "failed", "out of memory");
    reply->reply.result = r;
}

static const tas_cap_t *find_cap(tas_str_t id) {
    for (size_t i = 0; i < board->cap_count; i++) {
        if (tas_str_eq(id, board->caps[i].id)) return &board->caps[i];
    }
    return NULL;
}

void commands_init(const tas_board_t *b) {
    board = b;
}

void commands_handle(const tas_msg_t *cmd, tas_arena_t *arena, tas_msg_t *reply) {
    tas_str_t name = cmd->cmd.name;
    if (!cmd->cmd.has_cap) {
        if (tas_str_eq(name, "status")) return status(cmd->cmd.args, reply);
        if (tas_str_eq(name, "identify")) return identify(cmd->cmd.args, reply);
        if (tas_str_eq(name, "diag")) return diag(arena, reply);
        return tas_reply_fail(reply, "unsupported", "the device has no such command");
    }
    const tas_cap_t *cap = find_cap(cmd->cmd.cap);
    if (!cap) return tas_reply_fail(reply, "unsupported", "no such capability");
    const tas_kind_module_t *module = tas_kind_module(cap->kind);
    if (!module->command) {
        return tas_reply_fail(reply, "unsupported", "the capability has no such command");
    }
    module->command(cap, cmd, arena, reply);
}
