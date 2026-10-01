// The led Kind (docs/protocol/kinds/led.md): set/off/release on tas_led. Only the ctrl task
// changes a claim; the lock keeps the led task from rendering one half-changed.

#include <stdlib.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "kind_modules.h"

static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
static tas_kind_instances_t instances;
static tas_led_t leds[TAS_KINDS_MAX_INSTANCES];

static void init(const tas_cap_t *cap) {
    if (cap->count > TAS_LED_MAX) abort();
    tas_led_init(&leds[tas_kind_add(&instances, cap)], cap->count);
    ESP_LOGI("led", "%s ready", cap->id);
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)board;
    return tas_json_put(arena, tas_json_object(arena), "count", tas_json_int(arena, cap->count));
}

// Swaps in the claim `next`, made outside the lock so that no parsing holds it.
static void apply(tas_led_t *led, const tas_led_t *next) {
    portENTER_CRITICAL(&mux);
    *led = *next;
    portEXIT_CRITICAL(&mux);
}

static void command(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply) {
    (void)arena;
    tas_led_t *led = &leds[tas_kind_find(&instances, cap)];
    tas_led_t next = *led; // no lock: this task is the only writer
    tas_led_command(&next, cmd, (uint64_t)esp_timer_get_time() / 1000, reply);
    if (reply->reply.ok) apply(led, &next);
}

static void disconnected(const tas_cap_t *cap) {
    tas_led_t *led = &leds[tas_kind_find(&instances, cap)];
    tas_led_t next = *led;
    tas_led_release(&next);
    apply(led, &next);
}

void tas_kinds_led(const tas_cap_t *cap, tas_led_t *out) {
    const tas_led_t *led = &leds[tas_kind_find(&instances, cap)];
    portENTER_CRITICAL(&mux);
    *out = *led;
    portEXIT_CRITICAL(&mux);
}

const tas_kind_module_t tas_led_module = {
    .name = "led",
    .init = init,
    .params = params,
    .command = command,
    .disconnected = disconnected,
};
