// The Connection, driven as the ctrl task drives it: bytes from the hub in, the outbox out.

#include <stdio.h>
#include <string.h>
#include <unity.h>

#include "tas_conn.h"

#define T0 1000000ULL // the device clock when TCP came up
static tas_arena_t arena; // the test's own messages

// What the hooks saw.
static int tokens_stored, tokens_wiped, commands;
static char stored_token[TAS_MAX_TOKEN + 1];

static void on_token(void *ctx, const char *token) {
    (void)ctx;
    if (token) {
        tokens_stored++;
        strcpy(stored_token, token);
    } else {
        tokens_wiped++;
    }
}

static tas_conn_t conn;

// What `state` the last `status` asked for, read after any event it sent.
static char status_state[16];

// Answers `status` like the device does, and with `"event":true` sends an event first, as the
// device sends those queued before a command; fails anything else.
static void on_command(void *ctx, const tas_msg_t *cmd, tas_arena_t *a, tas_msg_t *reply) {
    (void)ctx;
    (void)a;
    commands++;
    if (tas_str_eq(cmd->cmd.name, "status")) {
        const tas_json_t *event = tas_json_get(cmd->cmd.args, "event");
        if (event && event->type == TAS_JSON_TRUE) {
            tas_conn_event(&conn, "button", "down", 6000, (const uint8_t *)"{}", 2);
        }
        const tas_json_t *state = tas_json_get(cmd->cmd.args, "state");
        snprintf(status_state, sizeof status_state, "%s", state ? state->text.ptr : "");
    } else {
        reply->reply.ok = false;
        reply->reply.code = tas_str("unsupported");
        reply->reply.message = tas_str("no such command");
    }
}

static const tas_conn_hooks_t HOOKS = {.command = on_command, .token = on_token};

static const tas_wire_cap_t CAPS[] = {{.id = {"button", 6}, .kind = {"button", 6}}};

static const tas_manifest_t MANIFEST = {
    .protocol = TAS_PROTOCOL,
    .device_id = {"a1b2c3d4e5f6", 12},
    .board = {"m5stack-atom-echo", 17},
    .firmware = {"0.1.0", 5},
    .caps = CAPS,
    .cap_count = 1,
};

static void open_link(const char *token) {
    tas_conn_open(&conn, &MANIFEST, token, 50123, &HOOKS, T0);
}

// Bytes from the hub, as one read.
static void hub_sends_bytes(const uint8_t *data, size_t len, uint64_t now) {
    size_t space;
    uint8_t *to = tas_conn_space(&conn, &space);
    TEST_ASSERT_TRUE(len <= space);
    memcpy(to, data, len);
    tas_conn_received(&conn, len, now);
}

// One JSON frame from the hub.
static void hub_says_at(const char *json, uint64_t now) {
    uint8_t frame[TAS_FRAME_HEADER + TAS_MAX_FRAME_BODY];
    size_t len = strlen(json);
    frame[0] = TAS_FRAME_JSON;
    frame[1] = 0;
    frame[2] = (uint8_t)len;
    frame[3] = (uint8_t)(len >> 8);
    memcpy(frame + TAS_FRAME_HEADER, json, len);
    hub_sends_bytes(frame, TAS_FRAME_HEADER + len, now);
}

static void hub_says(const char *json) {
    hub_says_at(json, T0);
}

// Takes the next message out of the outbox. False if it's empty.
static bool device_sent(tas_msg_t *msg) {
    size_t size;
    const uint8_t *out = tas_conn_outbox(&conn, &size);
    if (!size) return false;
    TEST_ASSERT_TRUE(size >= TAS_FRAME_HEADER);
    TEST_ASSERT_EQUAL_UINT8(TAS_FRAME_JSON, out[0]);
    size_t len = (size_t)(out[2] | out[3] << 8);
    TEST_ASSERT_TRUE(size >= TAS_FRAME_HEADER + len);
    TEST_ASSERT_EQUAL_INT(TAS_WIRE_OK, tas_msg_decode(out + TAS_FRAME_HEADER, len, &arena, msg));
    tas_conn_sent(&conn, TAS_FRAME_HEADER + len);
    return true;
}

static void expect_sent(tas_msg_type_t type, tas_msg_t *msg) {
    TEST_ASSERT_TRUE_MESSAGE(device_sent(msg), "nothing sent");
    TEST_ASSERT_EQUAL_INT(type, msg->type);
}

static void expect_nothing_sent(void) {
    size_t size;
    tas_conn_outbox(&conn, &size);
    TEST_ASSERT_EQUAL_size_t(0, size);
}

void setUp(void) {
    tokens_stored = tokens_wiped = commands = 0;
    stored_token[0] = 0;
}

void tearDown(void) {
    tas_conn_close(&conn, "test over");
    tas_arena_free(&arena);
}

static void test_an_unpaired_device_says_hello_with_its_manifest_and_no_token(void) {
    open_link(NULL);

    tas_msg_t hello;
    expect_sent(TAS_MSG_HELLO, &hello);
    TEST_ASSERT_FALSE(hello.hello.has_token);
    TEST_ASSERT_EQUAL_UINT16(50123, hello.hello.udp_port);
    TEST_ASSERT_TRUE(tas_str_eq(hello.hello.manifest.device_id, "a1b2c3d4e5f6"));
    TEST_ASSERT_TRUE(tas_str_eq(hello.hello.manifest.board, "m5stack-atom-echo"));
    TEST_ASSERT_EQUAL_size_t(1, hello.hello.manifest.cap_count);
    TEST_ASSERT_EQUAL_INT(TAS_CONN_HELLO, conn.state);
    expect_nothing_sent();
}

static void test_a_paired_device_says_hello_with_its_token(void) {
    open_link("tok-123");

    tas_msg_t hello;
    expect_sent(TAS_MSG_HELLO, &hello);
    TEST_ASSERT_TRUE(hello.hello.has_token);
    TEST_ASSERT_TRUE(tas_str_eq(hello.hello.token, "tok-123"));
}

static void test_a_ping_gets_a_pong_with_the_device_clock_even_before_welcome(void) {
    open_link(NULL);
    tas_msg_t msg;
    expect_sent(TAS_MSG_HELLO, &msg);

    hub_says_at("{\"type\":\"ping\",\"t\":777}", T0 + 2000);

    expect_sent(TAS_MSG_PONG, &msg);
    TEST_ASSERT_EQUAL_UINT64(777, msg.pong.t);
    TEST_ASSERT_EQUAL_UINT64(T0 + 2000, msg.pong.ts);
}

static void test_pending_waits_for_the_owner(void) {
    open_link(NULL);

    hub_says("{\"type\":\"pending\"}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_PENDING, conn.state);
}

static void test_welcome_after_pairing_stores_the_token(void) {
    open_link(NULL);
    hub_says("{\"type\":\"pending\"}");

    hub_says("{\"type\":\"welcome\",\"name\":\"kitchen\",\"conn_id\":42,\"udp_port\":8420,"
             "\"token\":\"new-token\"}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_WELCOMED, conn.state);
    TEST_ASSERT_EQUAL_STRING("kitchen", conn.name);
    TEST_ASSERT_EQUAL_UINT32(42, conn.conn_id);
    TEST_ASSERT_EQUAL_UINT16(8420, conn.hub_udp_port);
    TEST_ASSERT_EQUAL_INT(1, tokens_stored);
    TEST_ASSERT_EQUAL_STRING("new-token", stored_token);
}

static void test_welcome_with_no_token_keeps_the_stored_one(void) {
    open_link("tok-123");

    hub_says("{\"type\":\"welcome\",\"name\":\"kitchen\",\"conn_id\":7,\"udp_port\":8420}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_WELCOMED, conn.state);
    TEST_ASSERT_EQUAL_INT(0, tokens_stored + tokens_wiped);
}

static void test_unauthorized_wipes_the_token_and_closes(void) {
    open_link("stale");

    hub_says("{\"type\":\"error\",\"code\":\"unauthorized\",\"message\":\"bad token\","
             "\"protocol\":1}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
    TEST_ASSERT_EQUAL_INT(1, tokens_wiped);
}

static void test_any_other_hub_error_closes_and_keeps_the_token(void) {
    open_link("tok-123");

    hub_says("{\"type\":\"error\",\"code\":\"protocol_mismatch\",\"message\":\"no\","
             "\"protocol\":2}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
    TEST_ASSERT_EQUAL_INT(0, tokens_wiped);
}

static const char WELCOME[] =
    "{\"type\":\"welcome\",\"name\":\"kitchen\",\"conn_id\":7,\"udp_port\":8420}";

// Opened, hello taken, welcomed.
static void welcomed(void) {
    tas_msg_t hello;
    open_link("tok-123");
    expect_sent(TAS_MSG_HELLO, &hello);
    hub_says(WELCOME);
}

static void test_a_command_gets_a_reply_with_its_id(void) {
    welcomed();

    hub_says("{\"type\":\"cmd\",\"id\":5,\"name\":\"status\",\"args\":{\"state\":\"agent\"}}");
    hub_says("{\"type\":\"cmd\",\"id\":6,\"cap\":\"led\",\"name\":\"blink\",\"args\":{}}");

    tas_msg_t reply;
    expect_sent(TAS_MSG_REPLY, &reply);
    TEST_ASSERT_EQUAL_UINT32(5, reply.reply.id);
    TEST_ASSERT_TRUE(reply.reply.ok);
    TEST_ASSERT_EQUAL_size_t(0, reply.reply.result->items.count);
    expect_sent(TAS_MSG_REPLY, &reply);
    TEST_ASSERT_EQUAL_UINT32(6, reply.reply.id);
    TEST_ASSERT_FALSE(reply.reply.ok);
    TEST_ASSERT_TRUE(tas_str_eq(reply.reply.code, "unsupported"));
    TEST_ASSERT_EQUAL_INT(TAS_CONN_WELCOMED, conn.state);
}

static void test_a_command_before_welcome_is_a_protocol_error(void) {
    open_link(NULL);
    hub_says("{\"type\":\"pending\"}");

    hub_says("{\"type\":\"cmd\",\"id\":1,\"name\":\"status\",\"args\":{\"state\":\"agent\"}}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
    TEST_ASSERT_EQUAL_INT(0, commands);
}

// An event's data, as a Kind module writes it.
#define DRAINED "{\"stream\":3,\"played_ms\":1200,\"lost\":0,\"underruns\":0,\"underrun_ms\":0}"

static void test_events_go_out_in_order_counted_from_zero(void) {
    welcomed();

    tas_conn_event(&conn, "speaker", "drained", 5000, (const uint8_t *)DRAINED, strlen(DRAINED));
    tas_conn_event(&conn, "button", "down", 6000, (const uint8_t *)"{}", 2);

    tas_msg_t event;
    expect_sent(TAS_MSG_EVENT, &event);
    TEST_ASSERT_EQUAL_UINT32(0, event.event.seq);
    TEST_ASSERT_TRUE(tas_str_eq(event.event.cap, "speaker"));
    TEST_ASSERT_TRUE(tas_str_eq(event.event.name, "drained"));
    TEST_ASSERT_EQUAL_UINT64(5000, event.event.ts);
    TEST_ASSERT_EQUAL_INT64(1200, tas_json_get(event.event.data, "played_ms")->integer);
    TEST_ASSERT_EQUAL_size_t(5, event.event.data->items.count);
    expect_sent(TAS_MSG_EVENT, &event);
    TEST_ASSERT_EQUAL_UINT32(1, event.event.seq);
    TEST_ASSERT_TRUE(tas_str_eq(event.event.name, "down"));
    expect_nothing_sent();
}

static void test_an_event_sent_while_answering_a_command_goes_out_before_its_reply(void) {
    welcomed();

    hub_says("{\"type\":\"cmd\",\"id\":5,\"name\":\"status\","
             "\"args\":{\"event\":true,\"state\":\"agent\"}}");

    tas_msg_t msg;
    expect_sent(TAS_MSG_EVENT, &msg);
    TEST_ASSERT_TRUE(tas_str_eq(msg.event.name, "down"));
    expect_sent(TAS_MSG_REPLY, &msg);
    TEST_ASSERT_EQUAL_UINT32(5, msg.reply.id);
    TEST_ASSERT_EQUAL_STRING("agent", status_state); // the command outlived the event
}

static void test_no_event_goes_out_before_welcome(void) {
    tas_msg_t hello;
    open_link(NULL);
    expect_sent(TAS_MSG_HELLO, &hello);
    hub_says("{\"type\":\"pending\"}");

    tas_conn_event(&conn, "button", "down", 6000, (const uint8_t *)"{}", 2);

    expect_nothing_sent();
}

static void test_each_connection_counts_its_events_from_zero(void) {
    welcomed();
    tas_conn_event(&conn, "button", "down", 6000, (const uint8_t *)"{}", 2);
    tas_conn_close(&conn, "dropped");

    welcomed();
    tas_conn_event(&conn, "button", "up", 7000, (const uint8_t *)"{}", 2);

    tas_msg_t event;
    expect_sent(TAS_MSG_EVENT, &event);
    TEST_ASSERT_EQUAL_UINT32(0, event.event.seq);
    TEST_ASSERT_TRUE(tas_str_eq(event.event.name, "up"));
}

static void test_invalid_json_closes_the_connection(void) {
    welcomed();

    hub_says("{\"type\":\"ping\",\"t\":1,}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
}

static void test_a_bad_message_closes_the_connection(void) {
    welcomed();

    hub_says("{\"type\":\"cmd\",\"id\":1.0,\"name\":\"status\",\"args\":{}}");

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
    TEST_ASSERT_EQUAL_INT(0, commands);
}

static void test_a_frame_over_the_limit_closes_as_soon_as_its_header_is_in(void) {
    welcomed();
    static const uint8_t header[] = {TAS_FRAME_JSON, 0, 0x01, 0x10}; // len 4097

    hub_sends_bytes(header, sizeof header, T0);

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
}

static void test_unknown_messages_and_frame_types_are_skipped(void) {
    welcomed();
    static const uint8_t payload_chunk[] = {2, 1, 4, 0, 1, 0, 0, 0};

    hub_says("{\"type\":\"future\",\"x\":1}");
    hub_sends_bytes(payload_chunk, sizeof payload_chunk, T0);
    hub_says("{\"type\":\"ping\",\"t\":9}");

    tas_msg_t pong;
    expect_sent(TAS_MSG_PONG, &pong);
    TEST_ASSERT_EQUAL_UINT64(9, pong.pong.t);
    expect_nothing_sent();
    TEST_ASSERT_EQUAL_INT(TAS_CONN_WELCOMED, conn.state);
}

static void test_frames_split_across_reads_still_decode(void) {
    open_link(NULL);
    tas_msg_t msg;
    expect_sent(TAS_MSG_HELLO, &msg);
    static const char ping[] = "\x01\x00\x16\x00{\"type\":\"ping\",\"t\":12}";

    for (size_t i = 0; i < sizeof ping - 1; i++) {
        expect_nothing_sent();
        hub_sends_bytes((const uint8_t *)ping + i, 1, T0);
    }

    expect_sent(TAS_MSG_PONG, &msg);
    TEST_ASSERT_EQUAL_UINT64(12, msg.pong.t);
}

static void test_six_seconds_with_no_inbound_frame_is_a_dead_connection(void) {
    welcomed();

    tas_conn_tick(&conn, T0 + 5999999);
    TEST_ASSERT_EQUAL_INT(TAS_CONN_WELCOMED, conn.state);
    tas_conn_tick(&conn, T0 + 6000000);
    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
}

static void test_any_inbound_frame_keeps_the_connection_alive(void) {
    welcomed();
    static const uint8_t payload_chunk[] = {2, 1, 4, 0, 1, 0, 0, 0};

    hub_says_at("{\"type\":\"ping\",\"t\":1}", T0 + 4000000);
    hub_sends_bytes(payload_chunk, sizeof payload_chunk, T0 + 8000000);

    TEST_ASSERT_EQUAL_UINT64(T0 + 14000000, tas_conn_deadline(&conn));
    tas_conn_tick(&conn, T0 + 13999999);
    TEST_ASSERT_EQUAL_INT(TAS_CONN_WELCOMED, conn.state);
}

static void test_the_hub_closing_the_stream_closes_the_link(void) {
    welcomed();

    tas_conn_ended(&conn);

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
}

static void test_a_hub_that_stops_reading_fills_the_outbox_and_closes(void) {
    welcomed();

    for (int i = 0; i < 1000 && conn.state == TAS_CONN_WELCOMED; i++) {
        hub_says("{\"type\":\"ping\",\"t\":1}");
    }

    TEST_ASSERT_EQUAL_INT(TAS_CONN_CLOSED, conn.state);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_an_unpaired_device_says_hello_with_its_manifest_and_no_token);
    RUN_TEST(test_a_paired_device_says_hello_with_its_token);
    RUN_TEST(test_a_ping_gets_a_pong_with_the_device_clock_even_before_welcome);
    RUN_TEST(test_pending_waits_for_the_owner);
    RUN_TEST(test_welcome_after_pairing_stores_the_token);
    RUN_TEST(test_welcome_with_no_token_keeps_the_stored_one);
    RUN_TEST(test_unauthorized_wipes_the_token_and_closes);
    RUN_TEST(test_any_other_hub_error_closes_and_keeps_the_token);
    RUN_TEST(test_a_command_gets_a_reply_with_its_id);
    RUN_TEST(test_a_command_before_welcome_is_a_protocol_error);
    RUN_TEST(test_events_go_out_in_order_counted_from_zero);
    RUN_TEST(test_an_event_sent_while_answering_a_command_goes_out_before_its_reply);
    RUN_TEST(test_no_event_goes_out_before_welcome);
    RUN_TEST(test_each_connection_counts_its_events_from_zero);
    RUN_TEST(test_invalid_json_closes_the_connection);
    RUN_TEST(test_a_bad_message_closes_the_connection);
    RUN_TEST(test_a_frame_over_the_limit_closes_as_soon_as_its_header_is_in);
    RUN_TEST(test_unknown_messages_and_frame_types_are_skipped);
    RUN_TEST(test_frames_split_across_reads_still_decode);
    RUN_TEST(test_six_seconds_with_no_inbound_frame_is_a_dead_connection);
    RUN_TEST(test_any_inbound_frame_keeps_the_connection_alive);
    RUN_TEST(test_the_hub_closing_the_stream_closes_the_link);
    RUN_TEST(test_a_hub_that_stops_reading_fills_the_outbox_and_closes);
    return UNITY_END();
}
