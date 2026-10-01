// The speaker Kind (docs/protocol/kinds/speaker.md). The jitter buffer, ramps, low-cut, volume, drain/drained, played_ms and the cut.
//
// Each speaker's spk task plays its stream: the jitter buffer, the low-cut and the volume, then the
// Board's DMA ring, which the task refills as on_sent frees descriptors. The I2S channel runs from
// boot, playing silence while there's no stream: starting it can click (an amp waking up), and a
// cut needs a ring it has seen go round. A cut patches the ring in place (tas_cut).

#include <stdatomic.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "kind_modules.h"
#include "sdkconfig.h"
#include "tas_cut.h"
#include "tas_dsp.h"

#define TAG "speaker"
#define SPK_CORE 1
#define SPK_PRIORITY 19
#define SPK_STACK 3072
#define RAMP_MS 2
#define MAX_CHANNELS 2

typedef struct {
    const tas_cap_t *cap;
    tas_spk_cond_config_t cond_cfg;
    TaskHandle_t task;
    atomic_uchar volume; // set by the hub, never stored: each boot starts at the Kconfig default
    SemaphoreHandle_t lock; // everything below
    StaticSemaphore_t lock_buf;
    tas_cut_t cut;
    tas_jbuf_t jbuf;
    tas_spk_cond_t cond;
    volatile bool playing; // a stream is open; read by on_sent
    uint8_t stream;        // its id
    int16_t out[TAS_SPEAKER_MAX_FRAME * MAX_CHANNELS]; // a frame of playout, on every channel
    size_t out_len, out_pos;     // samples in out, and those already in the ring
    int64_t cut_us;              // when the last patched cut was called
    bool cut_pending;            // and it hasn't gone silent yet
    uint32_t silent_after_us;    // when it did, from cut_us; 0 until then
} speaker_t;

static tas_kind_instances_t instances;
static speaker_t speakers[CONFIG_TAS_SPEAKERS_MAX];

static speaker_t *speaker_of(const tas_cap_t *cap) {
    return &speakers[tas_kind_find(&instances, cap)];
}

// From the I2S ISR.
static bool on_sent(void *ctx, void *dma_buf) {
    speaker_t *s = ctx;
    bool reached = tas_cut_sent(&s->cut, dma_buf, esp_timer_get_time());
    if (!s->playing && !reached) return false;
    BaseType_t woke = pdFALSE;
    vTaskNotifyGiveFromISR(s->task, &woke);
    return woke == pdTRUE;
}

// The next frame of playout, into out: the jitter buffer, the low-cut, the volume, then every
// sample on each channel.
static void pull(speaker_t *s, float gain) {
    int16_t mono[TAS_SPEAKER_MAX_FRAME];
    size_t n = s->jbuf.frame_samples, channels = s->cap->speaker->channels;
    tas_jbuf_pull(&s->jbuf, mono);
    tas_spk_cond_run(&s->cond, mono, n);
    for (size_t i = 0; i < n; i++) {
        int16_t x = (int16_t)((float)mono[i] * gain);
        for (size_t c = 0; c < channels; c++) s->out[i * channels + c] = x;
    }
    s->out_len = n * channels;
    s->out_pos = 0;
}

// Writes playout into the ring while it has free descriptors.
static void fill(speaker_t *s, float gain) {
    const tas_speaker_hw_t *hw = s->cap->speaker;
    for (;;) {
        if (s->out_pos == s->out_len) {
            if (!s->playing) return;
            pull(s, gain);
        }
        if (!hw->write(s->out + s->out_pos)) return;
        s->out_pos += hw->frames * hw->channels;
    }
}

// True once, when the last patched cut has gone silent.
static bool cut_went_silent(speaker_t *s) {
    int64_t at;
    if (!s->cut_pending || !tas_cut_silent_at(&s->cut, &at)) return false;
    s->cut_pending = false;
    s->silent_after_us = (uint32_t)(at - s->cut_us);
    return true;
}

static void spk_task(void *arg) {
    speaker_t *s = arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    for (;;) {
        esp_task_wdt_reset();
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000));
        float gain = tas_volume_gain(atomic_load(&s->volume));
        xSemaphoreTake(s->lock, portMAX_DELAY);
        fill(s, gain);
        bool silent = cut_went_silent(s);
        uint32_t silent_after_us = s->silent_after_us;
        xSemaphoreGive(s->lock);
        if (silent) {
            ESP_LOGI(TAG, "%s cut: silent after %u us", s->cap->id, (unsigned)silent_after_us);
        }
    }
}

static void init(const tas_board_t *board, const tas_cap_t *cap) {
    size_t i = tas_kind_add(&instances, cap);
    if (i >= CONFIG_TAS_SPEAKERS_MAX) abort();
    speaker_t *s = &speakers[i];
    const tas_speaker_hw_t *hw = cap->speaker;
    s->cap = cap;
    s->cond_cfg = board->spk_cond;
    atomic_store(&s->volume, CONFIG_TAS_SPEAKER_VOLUME);
    s->lock = xSemaphoreCreateMutexStatic(&s->lock_buf);
    uint32_t rate = board->spk_cond.rate;
    tas_cut_config_t cut = {.descs = hw->descs,
                            .frames = hw->frames,
                            .channels = hw->channels,
                            .rate = rate,
                            .ramp = rate * RAMP_MS / 1000,
                            .fetch_lead = hw->fetch_lead};
    if (hw->channels > MAX_CHANNELS || !tas_cut_init(&s->cut, &cut)) abort();
    xTaskCreatePinnedToCore(spk_task, "spk", SPK_STACK, s, SPK_PRIORITY, &s->task, SPK_CORE);
    hw->open(on_sent, s);
    hw->start();
    ESP_LOGI(TAG, "%s ready", cap->id);
}

// Planning a patch and putting it in, with nothing in between: the plan holds for a fraction of a
// millisecond, and waiting for the lock or being preempted could take longer.
static portMUX_TYPE patch_lock = portMUX_INITIALIZER_UNLOCKED;

// As tas_speaker_cut, called at `called_us`, with the lock held.
static tas_speaker_cut_t cut_locked(speaker_t *s, bool hard, int64_t called_us) {
    if (!s->playing) return TAS_SPEAKER_IDLE;
    s->playing = false;
    s->out_len = s->out_pos = 0;
    s->silent_after_us = 0;
    s->cut_pending = false;
    if (!hard) {
        taskENTER_CRITICAL(&patch_lock);
        s->cut_pending = tas_cut_patch(&s->cut, esp_timer_get_time());
        taskEXIT_CRITICAL(&patch_lock);
    }
    if (s->cut_pending) {
        s->cut_us = called_us;
        return TAS_SPEAKER_PATCHED;
    }
    if (!hard) ESP_LOGW(TAG, "%s: the ring can't be patched: stopping the channel", s->cap->id);
    s->cap->speaker->stop();
    tas_cut_restart(&s->cut, esp_timer_get_time());
    s->cap->speaker->start();
    return TAS_SPEAKER_STOPPED;
}

bool tas_speaker_open(const tas_cap_t *cap, uint8_t stream, const tas_jbuf_config_t *cfg) {
    speaker_t *s = speaker_of(cap);
    if (cfg->rate != s->cond_cfg.rate) return false;
    xSemaphoreTake(s->lock, portMAX_DELAY);
    bool ok = !s->playing && tas_jbuf_start(&s->jbuf, cfg) &&
              s->jbuf.frame_samples <= TAS_SPEAKER_MAX_FRAME &&
              s->jbuf.frame_samples % cap->speaker->frames == 0;
    if (ok) {
        tas_spk_cond_start(&s->cond, &s->cond_cfg);
        s->stream = stream;
        s->playing = true;
    }
    xSemaphoreGive(s->lock);
    if (ok) xTaskNotifyGive(s->task);
    return ok;
}

bool tas_speaker_push(const tas_cap_t *cap, uint8_t stream, uint32_t seq, const int16_t *pcm,
                      size_t samples) {
    speaker_t *s = speaker_of(cap);
    xSemaphoreTake(s->lock, portMAX_DELAY);
    bool ok = s->playing && s->stream == stream && tas_jbuf_push(&s->jbuf, seq, pcm, samples);
    xSemaphoreGive(s->lock);
    return ok;
}

tas_speaker_cut_t tas_speaker_cut(const tas_cap_t *cap, bool hard) {
    int64_t called_us = esp_timer_get_time();
    speaker_t *s = speaker_of(cap);
    xSemaphoreTake(s->lock, portMAX_DELAY);
    tas_speaker_cut_t how = cut_locked(s, hard, called_us);
    xSemaphoreGive(s->lock);
    // The DMA may have reached the ramp while the patch was going in, before on_sent knew of it.
    if (how == TAS_SPEAKER_PATCHED) xTaskNotifyGive(s->task);
    return how;
}

bool tas_speaker_cut_silent_after(const tas_cap_t *cap, uint32_t *us) {
    speaker_t *s = speaker_of(cap);
    xSemaphoreTake(s->lock, portMAX_DELAY);
    *us = s->silent_after_us;
    xSemaphoreGive(s->lock);
    return *us != 0;
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
        atomic_store(&speaker_of(cap)->volume, (unsigned char)level);
        return;
    }
    // start, stop and drain come with the speaker's streams.
    tas_reply_fail(reply, "unsupported", "speaker has no such command yet");
}

uint8_t tas_kinds_volume(const tas_cap_t *cap) {
    return atomic_load(&speaker_of(cap)->volume);
}

const tas_kind_module_t tas_speaker_module = {
    .name = "speaker", .init = init, .params = params, .command = command};
