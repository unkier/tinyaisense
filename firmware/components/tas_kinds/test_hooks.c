// The debug builds' console commands (CONFIG_TAS_TEST_HOOKS), for checking the audio path on the
// device by ear and by the firmware's own timing:
//
//   tone [hz] [gaps]     a test tone on the speaker until cut; gaps holds back 100 ms in 500 ms
//   cut [disable]        cuts it by DMA patch, or by stopping the channel: the click to compare
//   cutbench [n]         n cuts (100) at random DMA phase, and how long they took
//   btn down|up          an edge on the button, as its interrupt would see it; down holds the
//                        pin down until up. The Reflex cut it makes logs how long it took.
//
// The tone goes the way a speaker stream will: a feeder task pushes 10 ms frames into the
// speaker's jitter buffer, paced as the hub paces (docs/protocol/audio.md): a 60 ms burst at the
// start and after each gap, then real time.

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_console.h"
#include "esp_random.h"
#include "esp_rom_sys.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "kind_modules.h"
#include "tas_stats.h"

#define FEEDER_CORE 0
#define FEEDER_PRIORITY 15
#define FEEDER_STACK 3072
#define FRAME_MS 10
#define BURST_FRAMES 6
#define TONE_HZ 1000      // small speakers barely play below ~500 Hz
#define TONE_LEVEL 16384  // -6 dBFS
#define GAP_EVERY 50      // frames
#define GAP_FRAMES 10
#define MAX_CUTS 200
#define CUT_TARGET_US 7000 // p99, from the call to silence

static const tas_board_t *board;
static const tas_cap_t *spk_cap; // the first speaker
static const tas_cap_t *btn_cap; // the first button

typedef struct {
    bool on, gaps;
    uint32_t hz;
    uint8_t stream; // a new tone is a new stream, so a frame of the last one can't get in
} tone_t;

static portMUX_TYPE tone_lock = portMUX_INITIALIZER_UNLOCKED;
static tone_t tone;

static const tas_cap_t *find(tas_kind_t kind) {
    for (size_t i = 0; i < board->cap_count; i++) {
        if (board->caps[i].kind == kind) return &board->caps[i];
    }
    return NULL;
}

static void feeder_task(void *arg) {
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    int16_t frame[TAS_KINDS_MAX_FRAME];
    uint8_t stream = 0;
    uint32_t seq = 0, tick = 0;
    bool burst = false;
    float phase = 0;
    TickType_t wake = xTaskGetTickCount();
    for (;;) {
        esp_task_wdt_reset();
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(FRAME_MS));
        taskENTER_CRITICAL(&tone_lock);
        tone_t cur = tone;
        taskEXIT_CRITICAL(&tone_lock);
        if (cur.stream != stream) {
            stream = cur.stream;
            seq = tick = 0;
            burst = true;
            phase = 0;
        }
        if (!cur.on) continue;
        if (cur.gaps && tick++ % GAP_EVERY >= GAP_EVERY - GAP_FRAMES) {
            burst = true;
            continue;
        }
        uint32_t rate = board->spk_cond.rate, samples = rate * FRAME_MS / 1000;
        float step = 2.0f * (float)M_PI * (float)cur.hz / (float)rate;
        for (uint32_t n = burst ? BURST_FRAMES : 1; n; n--) {
            for (uint32_t i = 0; i < samples; i++) {
                frame[i] = (int16_t)(TONE_LEVEL * sinf(phase));
                phase = fmodf(phase + step, 2.0f * (float)M_PI);
            }
            tas_speaker_push(spk_cap, stream, seq++, frame, samples);
        }
        burst = false;
    }
}

static void tone_stop(void) {
    taskENTER_CRITICAL(&tone_lock);
    tone.on = false;
    taskEXIT_CRITICAL(&tone_lock);
}

static bool tone_start(uint32_t hz, bool gaps) {
    tas_speaker_cut(spk_cap, false);
    tone_stop();
    uint8_t stream = tone.stream % 127 + 1; // the hub's range
    tas_jbuf_config_t cfg = {.rate = board->spk_cond.rate,
                             .frame_ms = FRAME_MS,
                             .prefill_ms = 40,
                             .capacity_ms = 200,
                             .ramp_ms = 2};
    if (!tas_speaker_open(spk_cap, stream, &cfg)) return false;
    taskENTER_CRITICAL(&tone_lock);
    tone = (tone_t){.on = true, .gaps = gaps, .hz = hz, .stream = stream};
    taskEXIT_CRITICAL(&tone_lock);
    return true;
}

static bool speaker_ready(void) {
    if (!spk_cap) printf("this Board has no speaker\n");
    return spk_cap;
}

static void print_ms(uint32_t us) {
    printf("%u.%02u", (unsigned)(us / 1000), (unsigned)(us % 1000 / 10));
}

static int cmd_tone(int argc, char **argv) {
    uint32_t hz = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 10) : TONE_HZ;
    bool gaps = argc > 2 && !strcmp(argv[2], "gaps");
    if (argc > 3 || hz < 20 || (argc > 2 && !gaps)) {
        printf("usage: tone [hz] [gaps]\n");
        return 1;
    }
    if (!speaker_ready()) return 1;
    if (hz >= board->spk_cond.rate / 2) {
        printf("%u Hz is past Nyquist\n", (unsigned)hz);
        return 1;
    }
    if (!tone_start(hz, gaps)) {
        printf("the speaker refused the stream: busy\n");
        return 1;
    }
    return 0;
}

static int cmd_cut(int argc, char **argv) {
    bool hard = argc == 2 && !strcmp(argv[1], "disable");
    if (argc > 2 || (argc == 2 && !hard)) {
        printf("usage: cut [disable]\n");
        return 1;
    }
    if (!speaker_ready()) return 1;
    tas_speaker_cut_t how = tas_speaker_cut(spk_cap, hard);
    tone_stop();
    // A patched cut's timing comes in the speaker's log.
    if (how == TAS_SPEAKER_IDLE) printf("no tone playing\n");
    else if (how == TAS_SPEAKER_STOPPED) printf("stopped the channel\n");
    return 0;
}

static void print_summary(uint32_t *us, size_t n) {
    static const struct {
        const char *name;
        unsigned pct;
    } stats[] = {{"min", 0}, {"p50", 50}, {"p95", 95}, {"p99", 99}, {"max", 100}};
    printf("silent after:");
    for (size_t i = 0; i < sizeof stats / sizeof *stats; i++) {
        printf(" %s ", stats[i].name);
        print_ms(tas_percentile(us, n, stats[i].pct));
    }
    printf(" ms\n");
}

static int cmd_cutbench(int argc, char **argv) {
    long n = argc > 1 ? strtol(argv[1], NULL, 10) : 100;
    if (argc > 2 || n < 1 || n > MAX_CUTS) {
        printf("usage: cutbench [1-%d]\n", MAX_CUTS);
        return 1;
    }
    if (!speaker_ready()) return 1;
    uint32_t desc_us = (uint32_t)(spk_cap->speaker->frames * 1000000 / board->spk_cond.rate);
    static uint32_t silent[MAX_CUTS];
    size_t timed = 0, stopped = 0, unreached = 0;
    for (long i = 0; i < n; i++) {
        esp_task_wdt_reset();
        if (!tone_start(TONE_HZ, false)) {
            printf("the speaker refused the stream\n");
            return 1;
        }
        vTaskDelay(pdMS_TO_TICKS(80)); // past the prefill, into steady playout
        // Anywhere in the descriptor playing, whatever the tick's phase against the DMA.
        esp_rom_delay_us(esp_random() % desc_us);
        tas_speaker_cut_t how = tas_speaker_cut(spk_cap, false);
        tone_stop();
        vTaskDelay(pdMS_TO_TICKS(20));
        if (how != TAS_SPEAKER_PATCHED) stopped++;
        else if (!tas_speaker_cut_silent_after(spk_cap, &silent[timed])) unreached++;
        else timed++;
    }
    printf("%u cuts timed; %u fell back to stopping the channel; %u never reached\n",
           (unsigned)timed, (unsigned)stopped, (unsigned)unreached);
    if (!timed) return 1;
    print_summary(silent, timed);
    bool pass = tas_percentile(silent, timed, 99) <= CUT_TARGET_US && !stopped && !unreached;
    printf("p99 <= %d ms: %s\n", CUT_TARGET_US / 1000, pass ? "pass" : "FAIL");
    return pass ? 0 : 1;
}

static int cmd_btn(int argc, char **argv) {
    bool down = argc == 2 && !strcmp(argv[1], "down");
    if (argc != 2 || (!down && strcmp(argv[1], "up"))) {
        printf("usage: btn down|up\n");
        return 1;
    }
    if (!btn_cap) {
        printf("this Board has no button\n");
        return 1;
    }
    tas_button_inject(btn_cap, down);
    return 0;
}

static const esp_console_cmd_t commands[] = {
    {.command = "tone", .help = "Play a test tone on the speaker until cut; gaps makes it underrun",
     .hint = "[hz] [gaps]", .func = cmd_tone},
    {.command = "cut", .help = "Cut the speaker by DMA patch, or by stopping the channel",
     .hint = "[disable]", .func = cmd_cut},
    {.command = "cutbench", .help = "Time cuts at random DMA phase", .hint = "[n]",
     .func = cmd_cutbench},
    {.command = "btn", .help = "Press or let go of the button, as its interrupt would see it",
     .hint = "down|up", .func = cmd_btn},
};

void tas_kinds_test_commands(const tas_board_t *b) {
    board = b;
    spk_cap = find(TAS_KIND_SPEAKER);
    btn_cap = find(TAS_KIND_BUTTON);
    for (size_t i = 0; i < sizeof commands / sizeof *commands; i++) {
        ESP_ERROR_CHECK(esp_console_cmd_register(&commands[i]));
    }
    if (spk_cap) {
        xTaskCreatePinnedToCore(feeder_task, "tone", FEEDER_STACK, NULL, FEEDER_PRIORITY, NULL,
                                FEEDER_CORE);
    }
}
