// The speaker Kind (docs/protocol/kinds/speaker.md). The jitter buffer, ramps, low-cut, volume, drain/drained, played_ms and the cut.
//
// Each speaker's spk task plays its stream: the jitter buffer, the low-cut and the volume, then the
// Board's DMA ring, which the task refills as on_sent frees descriptors. The I2S channel runs from
// boot, playing silence while there's no stream: starting it can click (an amp waking up), and a
// cut needs a ring it has seen go round. A cut patches the ring in place (tas_cut).
//
// The jitter buffer's stats are what it has played out; stop and drained report what was heard,
// which tas_playout tracks from what has left the DMA.

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
#include "tas_playout.h"

#define TAG "speaker"
#define SPK_CORE 1
#define SPK_PRIORITY 19
#define SPK_STACK 3072
#define RAMP_MS 2
#define MAX_CHANNELS 2
#define CAPACITY_MS 200
#define PREFILL_MS 40

typedef struct {
    const tas_cap_t *cap;
    tas_spk_cond_config_t cond_cfg;
    TaskHandle_t task;
    atomic_uchar volume; // set by the hub, never stored: each boot starts at the Kconfig default
    portMUX_TYPE playout_lock; // playout, which on_sent updates
    tas_playout_t playout;
    SemaphoreHandle_t lock; // everything below
    StaticSemaphore_t lock_buf;
    tas_cut_t cut;
    tas_jbuf_t jbuf;
    tas_spk_cond_t cond;
    volatile bool playing; // a stream is open; read by on_sent
    bool played_through;   // the jitter buffer played through the drain's last_seq
    uint8_t stream;        // its id
    int16_t out[TAS_KINDS_MAX_FRAME * MAX_CHANNELS]; // a frame of playout, on every channel
    size_t out_len, out_pos;     // samples in out, and those already in the ring
    tas_jbuf_stats_t out_stats;  // the stream's stats once out has played
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
    portENTER_CRITICAL_ISR(&s->playout_lock);
    tas_playout_sent(&s->playout, s->cut.sents);
    portEXIT_CRITICAL_ISR(&s->playout_lock);
    if (!s->playing && !reached) return false;
    BaseType_t woke = pdFALSE;
    vTaskNotifyGiveFromISR(s->task, &woke);
    return woke == pdTRUE;
}

// The stats of the stream's frames that have left the DMA.
static tas_jbuf_stats_t heard(speaker_t *s) {
    taskENTER_CRITICAL(&s->playout_lock);
    tas_jbuf_stats_t stats = tas_playout_heard(&s->playout);
    taskEXIT_CRITICAL(&s->playout_lock);
    return stats;
}

// The next frame of playout, into out: the jitter buffer, the low-cut, the volume, then every
// sample on each channel. False once the stream has played through its drain.
static bool pull(speaker_t *s, float gain) {
    int16_t mono[TAS_KINDS_MAX_FRAME];
    size_t n = s->jbuf.frame_samples, channels = s->cap->speaker->channels;
    if (tas_jbuf_pull(&s->jbuf, mono) == TAS_JBUF_DONE) return false;
    tas_spk_cond_run(&s->cond, mono, n);
    for (size_t i = 0; i < n; i++) {
        int16_t x = (int16_t)((float)mono[i] * gain);
        for (size_t c = 0; c < channels; c++) s->out[i * channels + c] = x;
    }
    s->out_len = n * channels;
    s->out_pos = 0;
    s->out_stats = tas_jbuf_stats(&s->jbuf);
    return true;
}

// Writes playout into the ring while it has free descriptors.
static void fill(speaker_t *s, float gain) {
    const tas_speaker_hw_t *hw = s->cap->speaker;
    for (;;) {
        if (s->out_pos == s->out_len) {
            if (!s->playing || s->played_through) return;
            if (!pull(s, gain)) {
                s->played_through = true;
                return;
            }
        }
        if (!hw->write(s->out + s->out_pos)) return;
        s->out_pos += hw->frames * hw->channels;
        // If on_sent freed a descriptor since the write, the tracker may take the write for a
        // later one: what was heard then shows up a descriptor late, never early.
        taskENTER_CRITICAL(&s->playout_lock);
        tas_playout_wrote(&s->playout);
        if (s->out_pos == s->out_len) tas_playout_frame(&s->playout, &s->out_stats);
        taskEXIT_CRITICAL(&s->playout_lock);
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

// The stream is over, its id dead. With the lock held.
static void end_stream(speaker_t *s) {
    s->playing = false;
    s->played_through = false;
    s->out_len = s->out_pos = 0;
    tas_kind_mark_closed(s->stream);
    tas_kind_release(s->cap);
}

// Emits `drained` for the open stream, with `stats`. With the lock held, so a command that comes
// after the stream ended finds the event queued.
static void emit_drained(const speaker_t *s, const tas_jbuf_stats_t *stats) {
    tas_kind_event_t event = {
        .cap = s->cap->id, .name = "drained", .ts = (uint64_t)esp_timer_get_time()};
    tas_jw_t w = {.buf = event.data, .cap = sizeof event.data};
    tas_jw_raw(&w, "{");
    tas_jw_key(&w, "stream", true);
    tas_jw_uint(&w, s->stream);
    tas_jw_key(&w, "played_ms", false);
    tas_jw_uint(&w, stats->played_ms);
    tas_jw_key(&w, "lost", false);
    tas_jw_uint(&w, stats->lost);
    tas_jw_key(&w, "underruns", false);
    tas_jw_uint(&w, stats->underruns);
    tas_jw_key(&w, "underrun_ms", false);
    tas_jw_uint(&w, stats->underrun_ms);
    tas_jw_raw(&w, "}");
    event.len = w.len;
    tas_kinds_core->emit(&event);
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
        taskENTER_CRITICAL(&s->playout_lock);
        bool played_out = s->playing && s->played_through && tas_playout_idle(&s->playout);
        taskEXIT_CRITICAL(&s->playout_lock);
        if (played_out) {
            // Everything pulled has left the DMA.
            tas_jbuf_stats_t stats = tas_jbuf_stats(&s->jbuf);
            emit_drained(s, &stats);
            end_stream(s);
        }
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
    portMUX_INITIALIZE(&s->playout_lock);
    s->lock = xSemaphoreCreateMutexStatic(&s->lock_buf);
    uint32_t rate = board->spk_cond.rate;
    tas_cut_config_t cut_cfg = {.descs = hw->descs,
                                .frames = hw->frames,
                                .channels = hw->channels,
                                .rate = rate,
                                .ramp = rate * RAMP_MS / 1000,
                                .fetch_lead = hw->fetch_lead};
    if (hw->channels > MAX_CHANNELS || !tas_cut_init(&s->cut, &cut_cfg) ||
        !tas_playout_init(&s->playout, hw->descs)) {
        abort();
    }
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
    end_stream(s);
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
    taskENTER_CRITICAL(&s->playout_lock);
    tas_playout_restart(&s->playout);
    taskEXIT_CRITICAL(&s->playout_lock);
    s->cap->speaker->start();
    return TAS_SPEAKER_STOPPED;
}

// As tas_speaker_cut, and the stats of what was heard of the stream it ended.
static tas_speaker_cut_t cut(speaker_t *s, bool hard, tas_jbuf_stats_t *stats) {
    int64_t called_us = esp_timer_get_time();
    xSemaphoreTake(s->lock, portMAX_DELAY);
    *stats = s->playing ? heard(s) : (tas_jbuf_stats_t){0};
    tas_speaker_cut_t how = cut_locked(s, hard, called_us);
    xSemaphoreGive(s->lock);
    // The DMA may have reached the ramp while the patch was going in, before on_sent knew of it.
    if (how == TAS_SPEAKER_PATCHED) xTaskNotifyGive(s->task);
    return how;
}

typedef enum {
    OPENED,
    PLAYING,   // a stream is open already
    PEER,      // an exclusive peer is active
    UNPLAYABLE // the jitter buffer can't take the config, or its frames don't split into
               // descriptors
} open_t;

static open_t open_stream(speaker_t *s, uint8_t stream, const tas_jbuf_config_t *cfg) {
    if (cfg->rate != s->cond_cfg.rate) return UNPLAYABLE;
    xSemaphoreTake(s->lock, portMAX_DELAY);
    open_t open = OPENED;
    if (s->playing) {
        open = PLAYING;
    } else if (!tas_jbuf_start(&s->jbuf, cfg) ||
               s->jbuf.frame_samples > TAS_KINDS_MAX_FRAME ||
               s->jbuf.frame_samples % s->cap->speaker->frames) {
        open = UNPLAYABLE;
    } else if (!tas_kind_acquire(s->cap)) {
        open = PEER;
    } else {
        tas_spk_cond_start(&s->cond, &s->cond_cfg);
        taskENTER_CRITICAL(&s->playout_lock);
        tas_playout_stream(&s->playout);
        taskEXIT_CRITICAL(&s->playout_lock);
        s->stream = stream;
        s->played_through = false;
        s->playing = true;
    }
    xSemaphoreGive(s->lock);
    if (open != OPENED) return open;
    // The prefill plays silence meanwhile; a mic peer is idle, so this doesn't wait.
    tas_kind_take_pins(s->cap);
    xTaskNotifyGive(s->task);
    return OPENED;
}

bool tas_speaker_open(const tas_cap_t *cap, uint8_t stream, const tas_jbuf_config_t *cfg) {
    return open_stream(speaker_of(cap), stream, cfg) == OPENED;
}

bool tas_speaker_push(const tas_cap_t *cap, uint8_t stream, uint32_t seq, const int16_t *pcm,
                      size_t samples) {
    speaker_t *s = speaker_of(cap);
    xSemaphoreTake(s->lock, portMAX_DELAY);
    bool ok = s->playing && s->stream == stream && tas_jbuf_push(&s->jbuf, seq, pcm, samples);
    xSemaphoreGive(s->lock);
    return ok;
}

static tas_frame_result_t frame_in(const tas_cap_t *cap, const tas_dgram_t *frame,
                                   tas_wire_err_t *err) {
    speaker_t *s = speaker_of(cap);
    const int16_t *pcm = (const int16_t *)(const void *)frame->payload;
    tas_frame_result_t result = TAS_FRAME_UNKNOWN;
    xSemaphoreTake(s->lock, portMAX_DELAY);
    if (s->playing && s->stream == frame->stream) {
        // A short frame may be the final one: the jitter buffer holds it until it knows.
        *err = tas_frame_check(frame->len, s->jbuf.rate, 1, s->jbuf.frame_ms, true);
        if (*err != TAS_WIRE_OK) result = TAS_FRAME_BAD;
        else if (tas_jbuf_push(&s->jbuf, frame->seq, pcm, frame->len / sizeof *pcm)) {
            result = TAS_FRAME_TAKEN;
        } else {
            result = TAS_FRAME_LATE;
        }
    }
    xSemaphoreGive(s->lock);
    return result;
}

tas_speaker_cut_t tas_speaker_cut(const tas_cap_t *cap, bool hard) {
    tas_jbuf_stats_t stats;
    return cut(speaker_of(cap), hard, &stats);
}

bool tas_speaker_cut_silent_after(const tas_cap_t *cap, uint32_t *us) {
    speaker_t *s = speaker_of(cap);
    xSemaphoreTake(s->lock, portMAX_DELAY);
    *us = s->silent_after_us;
    xSemaphoreGive(s->lock);
    return *us != 0;
}

// A cut has played out.
static bool quiet(const tas_cap_t *cap) {
    speaker_t *s = speaker_of(cap);
    int64_t at;
    xSemaphoreTake(s->lock, portMAX_DELAY);
    bool quiet = !s->playing && (!s->cut_pending || tas_cut_silent_at(&s->cut, &at));
    xSemaphoreGive(s->lock);
    return quiet;
}

static tas_json_t *params(const tas_board_t *board, const tas_cap_t *cap, tas_arena_t *arena) {
    (void)cap;
    return tas_kind_audio_params(board->spk_cond.rate, arena);
}

// `start{stream, format, prefill_ms?}` → `{format}`.
static void start(speaker_t *s, const tas_json_t *args, tas_arena_t *arena, tas_msg_t *reply) {
    uint8_t stream;
    uint32_t frame_ms;
    int64_t prefill_ms = PREFILL_MS;
    if (!tas_kind_stream_args(args, s->cond_cfg.rate, &stream, &frame_ms, reply)) return;
    if (tas_arg_int(args, "prefill_ms", 0, CAPACITY_MS, &prefill_ms) == TAS_ARG_BAD) {
        return tas_reply_fail(reply, "invalid_args", "prefill_ms must be an integer from 0 to 200");
    }
    tas_jbuf_config_t cfg = {.rate = s->cond_cfg.rate,
                             .frame_ms = frame_ms,
                             .prefill_ms = (uint32_t)prefill_ms,
                             .capacity_ms = CAPACITY_MS,
                             .ramp_ms = RAMP_MS};
    switch (open_stream(s, stream, &cfg)) {
    case OPENED: return tas_kind_format_reply(s->cond_cfg.rate, frame_ms, arena, reply);
    case PLAYING: return tas_reply_fail(reply, "busy", "the speaker is playing");
    case PEER: return tas_reply_fail(reply, "busy", "an exclusive peer is active");
    case UNPLAYABLE: return tas_reply_fail(reply, "failed", "the speaker can't play that stream");
    }
}

// `stop{}` → `{played_ms, lost, underruns, underrun_ms}`: what was heard.
static void stop(speaker_t *s, tas_arena_t *a, tas_msg_t *reply) {
    tas_jbuf_stats_t stats;
    cut(s, false, &stats);
    tas_json_t *r = tas_json_object(a);
    r = tas_json_put(a, r, "played_ms", tas_json_int(a, stats.played_ms));
    r = tas_json_put(a, r, "lost", tas_json_int(a, stats.lost));
    r = tas_json_put(a, r, "underruns", tas_json_int(a, stats.underruns));
    r = tas_json_put(a, r, "underrun_ms", tas_json_int(a, stats.underrun_ms));
    if (!r) return tas_reply_fail(reply, "failed", "out of memory");
    reply->reply.result = r;
}

// `drain{last_seq}` → `{}`, then `drained` once the stream has played out.
static void drain(speaker_t *s, const tas_json_t *args, tas_msg_t *reply) {
    const tas_json_t *arg = tas_json_get(args, "last_seq");
    bool none = arg && arg->type == TAS_JSON_NULL;
    int64_t last_seq = 0;
    if (!none && tas_arg_int(args, "last_seq", 0, UINT32_MAX, &last_seq) != TAS_ARG_OK) {
        return tas_reply_fail(reply, "invalid_args",
                              "last_seq must be null or an integer from 0 to 4294967295");
    }
    xSemaphoreTake(s->lock, portMAX_DELAY);
    if (s->playing && !s->played_through) {
        if (none) {
            // No frames: nothing to wait for.
            tas_jbuf_stats_t stats = tas_jbuf_stats(&s->jbuf);
            emit_drained(s, &stats);
            end_stream(s);
        } else {
            tas_jbuf_drain(&s->jbuf, (uint32_t)last_seq);
        }
    }
    xSemaphoreGive(s->lock);
    xTaskNotifyGive(s->task); // a drain ends the prefill
}

static void command(const tas_cap_t *cap, const tas_msg_t *cmd, tas_arena_t *arena,
                    tas_msg_t *reply) {
    speaker_t *s = speaker_of(cap);
    tas_str_t name = cmd->cmd.name;
    if (tas_str_eq(name, "start")) return start(s, cmd->cmd.args, arena, reply);
    if (tas_str_eq(name, "stop")) return stop(s, arena, reply);
    if (tas_str_eq(name, "drain")) return drain(s, cmd->cmd.args, reply);
    if (tas_str_eq(name, "set_volume")) {
        int64_t level;
        if (tas_arg_int(cmd->cmd.args, "level", 0, 100, &level) != TAS_ARG_OK) {
            return tas_reply_fail(reply, "invalid_args", "level must be an integer from 0 to 100");
        }
        atomic_store(&s->volume, (unsigned char)level);
        return;
    }
    tas_reply_fail(reply, "unsupported", "speaker has no such command");
}

// The Connection's stream ends with it, and nobody hears of it.
static void disconnected(const tas_cap_t *cap) {
    tas_jbuf_stats_t stats;
    cut(speaker_of(cap), false, &stats);
}

uint8_t tas_kinds_volume(const tas_cap_t *cap) {
    return atomic_load(&speaker_of(cap)->volume);
}

const tas_kind_module_t tas_speaker_module = {.name = "speaker",
                                              .init = init,
                                              .params = params,
                                              .command = command,
                                              .disconnected = disconnected,
                                              .frame_in = frame_in,
                                              .quiet = quiet};
