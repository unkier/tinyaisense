// One button's Reflex on a half-duplex test device, which logs what the Reflex does to it: the
// cuts, the mic opened and closed, and the events in the order they're queued. Each case is one
// the Simulator handles (packages/sim/src/tinyaisense_sim/simulator.py, "The button").

#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "tas_reflex.h"

static const tas_cap_t caps[] = {
    {.id = "mic", .kind = TAS_KIND_MIC},
    {.id = "speaker", .kind = TAS_KIND_SPEAKER},
    {.id = "button", .kind = TAS_KIND_BUTTON},
    {.id = "led", .kind = TAS_KIND_LED, .count = 1},
    {.id = "mic2", .kind = TAS_KIND_MIC}, // outside the exclusive group
};
#define MIC (&caps[0])
#define SPEAKER (&caps[1])
#define BUTTON (&caps[2])
#define MIC2 (&caps[4])

static const char *const half_duplex[] = {"mic", "speaker"};
static const tas_group_t exclusive[] = {{half_duplex, 2}};
static const tas_board_t board = {.name = "test",
                                  .caps = caps,
                                  .cap_count = sizeof caps / sizeof *caps,
                                  .exclusive = exclusive,
                                  .exclusive_count = 1,
                                  .mic_cond = {.rate = 16000}};
static const uint32_t frame_ms[] = {10, 20};

#define ARMED                                                                                      \
    "{\"armed\":true,\"stop\":[\"speaker\"],\"start\":{\"cap\":\"mic\",\"format\":"             \
    "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":1,\"frame_ms\":10}}}"

#define LOG_MAX 32
#define LINE 200

// The test device.
static struct {
    bool online;                          // a Connection is welcomed
    bool playing;                         // the speaker has a stream open
    uint8_t speaker_stream;
    tas_jbuf_stats_t heard;               // what a cut heard of it
    bool mic_open;
    uint8_t mic_stream;
    tas_mic_end_t mic_end;                // what a close reports
    bool mic_refuses;                     // an exclusive peer took the mic meanwhile
    bool mic2_open;                       // the other mic, e.g. another button's Reflex's
    uint8_t mic2_stream;
    char log[LOG_MAX][LINE];
    size_t logged;
} dev;

static tas_reflex_t reflex;
static tas_arena_t arena;
static tas_msg_t reply;

static void note(const char *line) {
    TEST_ASSERT_LESS_THAN(LOG_MAX, dev.logged);
    snprintf(dev.log[dev.logged++], LINE, "%s", line);
}

static bool dev_cut(void *ctx, const tas_cap_t *cap, uint64_t edge_us, uint8_t *stream,
                    tas_jbuf_stats_t *heard) {
    (void)ctx;
    char line[LINE];
    snprintf(line, sizeof line, "cut %s, timed from %llu", cap->id, (unsigned long long)edge_us);
    note(line);
    if (cap != SPEAKER || !dev.playing) return false;
    dev.playing = false;
    *stream = dev.speaker_stream;
    *heard = dev.heard;
    return true;
}

static bool dev_mic_stream(void *ctx, const tas_cap_t *cap, uint8_t *stream) {
    (void)ctx;
    if (cap == MIC2) {
        *stream = dev.mic2_stream;
        return dev.mic2_open;
    }
    TEST_ASSERT_EQUAL_PTR(MIC, cap);
    *stream = dev.mic_stream;
    return dev.mic_open;
}

static bool dev_mic_open(void *ctx, const tas_cap_t *cap, uint8_t stream, uint32_t ms) {
    (void)ctx;
    char line[LINE];
    snprintf(line, sizeof line, "open %s on %u, %u ms frames", cap->id, stream, (unsigned)ms);
    note(line);
    if (dev.mic_open || dev.mic_refuses || dev.playing) return false;
    dev.mic_open = true;
    dev.mic_stream = stream;
    return true;
}

static bool dev_mic_close(void *ctx, const tas_cap_t *cap, uint8_t stream, tas_mic_end_t *end) {
    (void)ctx;
    char line[LINE];
    snprintf(line, sizeof line, "close %s's %u", cap->id, stream);
    note(line);
    if (!dev.mic_open || dev.mic_stream != stream) return false;
    dev.mic_open = false;
    *end = dev.mic_end;
    return true;
}

static bool dev_emit(void *ctx, const char *cap, const char *name, uint64_t ts,
                     const uint8_t *data, size_t len, bool mirror) {
    (void)ctx;
    if (!dev.online) return false;
    char line[LINE];
    snprintf(line, sizeof line, "%s.%s at %llu %.*s%s", cap, name, (unsigned long long)ts,
             (int)len, (const char *)data, mirror ? ", mirrored" : "");
    note(line);
    return true;
}

void setUp(void) {
    memset(&dev, 0, sizeof dev);
    dev.online = true;
    tas_reflex_config_t cfg = {.board = &board,
                               .button = BUTTON,
                               .frame_ms = frame_ms,
                               .frame_ms_count = 2,
                               .ops = {.cut = dev_cut,
                                       .mic_stream = dev_mic_stream,
                                       .mic_open = dev_mic_open,
                                       .mic_close = dev_mic_close,
                                       .emit = dev_emit}};
    tas_reflex_init(&reflex, &cfg);
}

void tearDown(void) {
    tas_arena_free(&arena);
}

// The hub's set_reflex with `args`, a JSON object; the reply lands in `reply`.
static void set_reflex(const char *args) {
    tas_json_t *parsed;
    TEST_ASSERT_EQUAL_INT(TAS_JSON_OK,
                          tas_json_parse((const uint8_t *)args, strlen(args), &arena, &parsed));
    reply = (tas_msg_t){.type = TAS_MSG_REPLY};
    reply.reply.ok = true;
    reply.reply.result = tas_json_object(&arena);
    tas_reflex_set(&reflex, parsed, &reply);
}

static void arm(void) {
    set_reflex(ARMED);
    TEST_ASSERT_TRUE(reply.reply.ok);
}

static void edge(bool down, uint64_t ts, uint64_t now_us) {
    tas_edge_t e = {.down = down, .ts = ts};
    tas_reflex_edge(&reflex, &e, now_us);
}

static void play(uint8_t stream) {
    dev.playing = true;
    dev.speaker_stream = stream;
}

// The log is `lines`, NULL-terminated; then it's cleared.
static void expect_log(const char *const *lines) {
    size_t n = 0;
    for (; lines[n]; n++) {
        char where[32];
        snprintf(where, sizeof where, "log line %u", (unsigned)n);
        TEST_ASSERT_LESS_THAN_MESSAGE(dev.logged, n, where);
        TEST_ASSERT_EQUAL_STRING_MESSAGE(lines[n], dev.log[n], where);
    }
    if (dev.logged > n) TEST_FAIL_MESSAGE(dev.log[n]); // the first line not expected
    dev.logged = 0;
}

#define EXPECT_LOG(...) expect_log((const char *const[]){__VA_ARGS__, NULL})

static void test_an_armed_press_cuts_the_speaker_then_opens_the_mic(void) {
    arm();
    play(3);
    dev.heard = (tas_jbuf_stats_t){.played_ms = 1200, .lost = 2, .underruns = 1, .underrun_ms = 30};
    edge(true, 5000, 6000);
    EXPECT_LOG("cut speaker, timed from 5000",
               "button.down at 5000 {}, mirrored",
               "speaker.stopped at 6000 {\"stream\":3,\"reason\":\"reflex\",\"played_ms\":1200,"
               "\"lost\":2,\"underruns\":1,\"underrun_ms\":30}",
               "open mic on 128, 10 ms frames",
               "mic.started at 6000 {\"stream\":128,\"format\":{\"codec\":\"pcm_s16le\","
               "\"rate\":16000,\"channels\":1,\"frame_ms\":10}}");
}

static void test_the_release_closes_the_mic_the_press_opened(void) {
    arm();
    edge(true, 5000, 6000);
    dev.logged = 0;
    dev.mic_end = (tas_mic_end_t){.any = true, .last_seq = 41, .dropped = 2};
    edge(false, 900000, 901000);
    EXPECT_LOG("button.up at 900000 {}, mirrored",
               "close mic's 128",
               "mic.stopped at 901000 {\"stream\":128,\"reason\":\"reflex\",\"last_seq\":41,"
               "\"dropped\":2}");
}

static void test_a_mic_stopped_before_its_first_frame_has_no_last_seq(void) {
    arm();
    edge(true, 5000, 6000);
    dev.logged = 0;
    dev.mic_end = (tas_mic_end_t){.any = false};
    edge(false, 9000, 9500);
    EXPECT_LOG("button.up at 9000 {}, mirrored",
               "close mic's 128",
               "mic.stopped at 9500 {\"stream\":128,\"reason\":\"reflex\",\"last_seq\":null,"
               "\"dropped\":0}");
}

static void test_a_disarmed_press_sends_only_its_edges(void) {
    play(3);
    edge(true, 5000, 6000);
    edge(false, 9000, 9500);
    EXPECT_LOG("button.down at 5000 {}, mirrored", "button.up at 9000 {}, mirrored");
    TEST_ASSERT_TRUE(dev.playing);
}

static void test_a_mic_already_streaming_for_the_hub_is_left_alone(void) {
    arm();
    dev.mic_open = true;
    dev.mic_stream = 7;
    edge(true, 5000, 6000);
    edge(false, 9000, 9500);
    EXPECT_LOG("button.down at 5000 {}, mirrored", "button.up at 9000 {}, mirrored");
    TEST_ASSERT_TRUE(dev.mic_open);
}

static void test_a_mic_the_hub_stopped_during_the_hold_sends_no_stopped(void) {
    arm();
    edge(true, 5000, 6000);
    dev.mic_open = false; // the hub's stop: a click, or the 60 s cap
    dev.logged = 0;
    edge(false, 9000, 9500);
    EXPECT_LOG("button.up at 9000 {}, mirrored", "close mic's 128");
}

static void test_disarming_during_the_hold_still_closes_the_mic_on_release(void) {
    arm();
    edge(true, 5000, 6000);
    set_reflex("{\"armed\":false}");
    TEST_ASSERT_TRUE(reply.reply.ok);
    dev.logged = 0;
    edge(false, 9000, 9500);
    EXPECT_LOG("button.up at 9000 {}, mirrored",
               "close mic's 128",
               "mic.stopped at 9500 {\"stream\":128,\"reason\":\"reflex\",\"last_seq\":null,"
               "\"dropped\":0}");
}

static void test_a_button_held_across_a_reconnect_is_ignored_until_the_next_press(void) {
    arm();
    edge(true, 5000, 6000);
    tas_reflex_disconnected(&reflex);
    dev.mic_open = false; // the drop ended the stream
    dev.logged = 0;
    edge(false, 9000, 9500);
    EXPECT_LOG(NULL);
    edge(true, 20000, 21000);
    edge(false, 30000, 31000);
    EXPECT_LOG("button.down at 20000 {}, mirrored", "button.up at 30000 {}, mirrored");
}

static void test_a_press_with_no_connection_sends_nothing_nor_does_its_release(void) {
    dev.online = false;
    edge(true, 5000, 6000);
    dev.online = true; // welcomed during the hold
    edge(false, 9000, 9500);
    EXPECT_LOG(NULL);
}

static void test_a_drop_disarms_the_reflex(void) {
    arm();
    tas_reflex_disconnected(&reflex);
    play(3);
    edge(true, 5000, 6000);
    EXPECT_LOG("button.down at 5000 {}, mirrored");
    TEST_ASSERT_TRUE(dev.playing);
}

static void test_the_mic_s_exclusive_peers_are_cut_even_if_not_named(void) {
    set_reflex("{\"armed\":true,\"start\":{\"cap\":\"mic\",\"format\":"
               "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":1,\"frame_ms\":20}}}");
    TEST_ASSERT_TRUE(reply.reply.ok);
    play(3);
    edge(true, 5000, 6000);
    EXPECT_LOG("cut speaker, timed from 5000",
               "button.down at 5000 {}, mirrored",
               "speaker.stopped at 6000 {\"stream\":3,\"reason\":\"reflex\",\"played_ms\":0,"
               "\"lost\":0,\"underruns\":0,\"underrun_ms\":0}",
               "open mic on 128, 20 ms frames",
               "mic.started at 6000 {\"stream\":128,\"format\":{\"codec\":\"pcm_s16le\","
               "\"rate\":16000,\"channels\":1,\"frame_ms\":20}}");
}

static void test_an_idle_speaker_sends_no_stopped(void) {
    arm();
    edge(true, 5000, 6000);
    EXPECT_LOG("cut speaker, timed from 5000",
               "button.down at 5000 {}, mirrored",
               "open mic on 128, 10 ms frames",
               "mic.started at 6000 {\"stream\":128,\"format\":{\"codec\":\"pcm_s16le\","
               "\"rate\":16000,\"channels\":1,\"frame_ms\":10}}");
}

static void test_a_mic_that_refuses_to_open_sends_no_started(void) {
    arm();
    dev.mic_refuses = true;
    edge(true, 5000, 6000);
    edge(false, 9000, 9500);
    EXPECT_LOG("cut speaker, timed from 5000",
               "button.down at 5000 {}, mirrored",
               "open mic on 128, 10 ms frames",
               "button.up at 9000 {}, mirrored");
}

static void test_device_stream_ids_go_round_robin_and_wrap(void) {
    arm();
    for (unsigned id = 128; id <= 255; id++) {
        edge(true, 1000 * id, 1000 * id);
        TEST_ASSERT_EQUAL_UINT8(id, dev.mic_stream);
        edge(false, 1000 * id + 500, 1000 * id + 500);
        dev.logged = 0;
    }
    edge(true, 900000, 900000);
    TEST_ASSERT_EQUAL_UINT8(128, dev.mic_stream);
}

static void test_device_stream_ids_skip_one_a_mic_has_open(void) {
    arm();
    dev.mic2_open = true;
    dev.mic2_stream = 128;
    edge(true, 5000, 6000);
    TEST_ASSERT_EQUAL_UINT8(129, dev.mic_stream);
}

#define FORMAT_10 "{\"codec\":\"pcm_s16le\",\"rate\":16000,\"channels\":1,\"frame_ms\":10}"

static void test_set_reflex_refuses_args_that_break_the_contract_or_the_board(void) {
    static const char *const refused[] = {
        "{}",
        "{\"armed\":1}",
        "{\"armed\":true,\"stop\":\"speaker\"}",
        "{\"armed\":true,\"stop\":[1]}",
        "{\"armed\":true,\"stop\":[\"led\"]}",          // no speaker
        "{\"armed\":true,\"stop\":[\"nope\"]}",         // no capability
        "{\"armed\":true,\"stop\":[\"speaker\",\"speaker\",\"speaker\",\"speaker\","
        "\"speaker\",\"speaker\",\"speaker\",\"speaker\",\"speaker\"]}", // past the most
        "{\"armed\":true,\"start\":[]}",
        "{\"armed\":true,\"start\":{\"format\":" FORMAT_10 "}}",
        "{\"armed\":true,\"start\":{\"cap\":\"mic\"}}",
        "{\"armed\":true,\"start\":{\"cap\":\"speaker\",\"format\":" FORMAT_10 "}}",
        "{\"armed\":true,\"start\":{\"cap\":\"mic\",\"format\":{\"codec\":\"pcm_s16le\","
        "\"rate\":48000,\"channels\":1,\"frame_ms\":10}}}", // not advertised
        "{\"armed\":true,\"start\":{\"cap\":\"mic\",\"format\":{\"codec\":\"pcm_s16le\","
        "\"rate\":16000,\"channels\":1,\"frame_ms\":30}}}",
        "{\"armed\":false,\"stop\":[1]}",
        "{\"armed\":false,\"start\":{\"cap\":\"mic\",\"format\":{\"codec\":\"pcm_s16le\","
        "\"rate\":\"16000\",\"channels\":1,\"frame_ms\":10}}}",
        "{\"armed\":false,\"start\":{\"cap\":\"mic\",\"format\":{\"codec\":\"pcm_s16le\","
        "\"rate\":16000,\"channels\":1}}}",
    };
    for (size_t i = 0; i < sizeof refused / sizeof *refused; i++) {
        set_reflex(refused[i]);
        TEST_ASSERT_FALSE_MESSAGE(reply.reply.ok, refused[i]);
        TEST_ASSERT_TRUE_MESSAGE(tas_str_eq(reply.reply.code, "invalid_args"), refused[i]);
    }
}

static void test_a_refused_set_reflex_leaves_the_reflex_as_it_was(void) {
    arm();
    set_reflex("{\"armed\":true,\"stop\":[\"led\"]}");
    TEST_ASSERT_FALSE(reply.reply.ok);
    play(3);
    edge(true, 5000, 6000);
    TEST_ASSERT_FALSE(dev.playing);
    TEST_ASSERT_TRUE(dev.mic_open);
}

static void test_disarming_ignores_what_it_would_cut_or_open(void) {
    arm();
    set_reflex("{\"armed\":false,\"stop\":[\"led\"]}");
    TEST_ASSERT_TRUE(reply.reply.ok);
    play(3);
    edge(true, 5000, 6000);
    EXPECT_LOG("button.down at 5000 {}, mirrored");
}

static void test_arming_with_nothing_to_cut_or_open_sends_only_edges(void) {
    set_reflex("{\"armed\":true}");
    TEST_ASSERT_TRUE(reply.reply.ok);
    play(3);
    edge(true, 5000, 6000);
    EXPECT_LOG("button.down at 5000 {}, mirrored");
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_an_armed_press_cuts_the_speaker_then_opens_the_mic);
    RUN_TEST(test_the_release_closes_the_mic_the_press_opened);
    RUN_TEST(test_a_mic_stopped_before_its_first_frame_has_no_last_seq);
    RUN_TEST(test_a_disarmed_press_sends_only_its_edges);
    RUN_TEST(test_a_mic_already_streaming_for_the_hub_is_left_alone);
    RUN_TEST(test_a_mic_the_hub_stopped_during_the_hold_sends_no_stopped);
    RUN_TEST(test_disarming_during_the_hold_still_closes_the_mic_on_release);
    RUN_TEST(test_a_button_held_across_a_reconnect_is_ignored_until_the_next_press);
    RUN_TEST(test_a_press_with_no_connection_sends_nothing_nor_does_its_release);
    RUN_TEST(test_a_drop_disarms_the_reflex);
    RUN_TEST(test_the_mic_s_exclusive_peers_are_cut_even_if_not_named);
    RUN_TEST(test_an_idle_speaker_sends_no_stopped);
    RUN_TEST(test_a_mic_that_refuses_to_open_sends_no_started);
    RUN_TEST(test_device_stream_ids_go_round_robin_and_wrap);
    RUN_TEST(test_device_stream_ids_skip_one_a_mic_has_open);
    RUN_TEST(test_set_reflex_refuses_args_that_break_the_contract_or_the_board);
    RUN_TEST(test_a_refused_set_reflex_leaves_the_reflex_as_it_was);
    RUN_TEST(test_disarming_ignores_what_it_would_cut_or_open);
    RUN_TEST(test_arming_with_nothing_to_cut_or_open_sends_only_edges);
    return UNITY_END();
}
