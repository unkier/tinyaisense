#include "tas_conn.h"

#include <string.h>

static void close_for(tas_conn_t *conn, const char *why) {
    if (conn->state != TAS_CONN_CLOSED) {
        conn->state = TAS_CONN_CLOSED;
        conn->why = why;
    }
}

// Queues `msg` as a frame. Closes the Connection if the outbox is full: the hub isn't reading.
static void send(tas_conn_t *conn, const tas_msg_t *msg) {
    int len = tas_msg_frame(msg, conn->out + conn->out_len, sizeof conn->out - conn->out_len);
    if (len < 0) close_for(conn, "outbox full");
    else conn->out_len += (size_t)len;
}

void tas_conn_open(tas_conn_t *conn, const tas_manifest_t *manifest, const char *token,
                   uint16_t udp_port, const tas_conn_hooks_t *hooks, uint64_t now_us) {
    *conn = (tas_conn_t){.state = TAS_CONN_HELLO, .hooks = *hooks, .last_rx_us = now_us};
    conn->arena.limit = TAS_CONN_ARENA_LIMIT;
    tas_msg_t hello = {.type = TAS_MSG_HELLO};
    hello.hello.has_token = token != NULL;
    if (token) hello.hello.token = tas_str(token);
    hello.hello.udp_port = udp_port;
    hello.hello.manifest = *manifest;
    send(conn, &hello);
}

static void welcome(tas_conn_t *conn, const tas_msg_t *msg) {
    size_t len = msg->welcome.name.len;
    if (len > TAS_CONN_MAX_NAME) len = TAS_CONN_MAX_NAME;
    memcpy(conn->name, msg->welcome.name.ptr, len);
    conn->name[len] = 0;
    conn->conn_id = msg->welcome.conn_id;
    conn->hub_udp_port = msg->welcome.udp_port;
    conn->state = TAS_CONN_WELCOMED;
    if (msg->welcome.has_token) conn->hooks.token(conn->hooks.ctx, msg->welcome.token.ptr);
}

static void command(tas_conn_t *conn, const tas_msg_t *cmd) {
    tas_msg_t reply = {.type = TAS_MSG_REPLY};
    reply.reply.id = cmd->cmd.id;
    reply.reply.ok = true;
    reply.reply.result = tas_json_object(&conn->arena);
    if (!reply.reply.result) return close_for(conn, "no memory");
    conn->hooks.command(conn->hooks.ctx, cmd, &conn->arena, &reply);
    send(conn, &reply);
}

static void handle(tas_conn_t *conn, const tas_msg_t *msg, uint64_t now_us) {
    bool welcomed = conn->state == TAS_CONN_WELCOMED;
    switch (msg->type) {
    case TAS_MSG_PING: {
        tas_msg_t pong = {.type = TAS_MSG_PONG};
        pong.pong.t = msg->ping.t;
        pong.pong.ts = now_us;
        return send(conn, &pong);
    }
    case TAS_MSG_PENDING:
        if (!welcomed) conn->state = TAS_CONN_PENDING;
        return;
    case TAS_MSG_WELCOME:
        if (!welcomed) welcome(conn, msg);
        return;
    case TAS_MSG_ERROR:
        if (tas_str_eq(msg->error.code, "unauthorized")) {
            conn->hooks.token(conn->hooks.ctx, NULL);
            return close_for(conn, "unauthorized");
        }
        return close_for(conn, "hub error");
    case TAS_MSG_NONE: return;
    default:
        // Nothing else may come before welcome. After it, the hub only sends commands.
        if (!welcomed) return close_for(conn, "message before welcome");
        if (msg->type == TAS_MSG_CMD) command(conn, msg);
        return;
    }
}

uint8_t *tas_conn_space(tas_conn_t *conn, size_t *size) {
    return tas_framer_space(&conn->framer, size);
}

void tas_conn_received(tas_conn_t *conn, size_t size, uint64_t now_us) {
    tas_framer_commit(&conn->framer, size);
    while (conn->state != TAS_CONN_CLOSED) {
        tas_frame_t frame;
        bool got;
        tas_wire_err_t err = tas_framer_next(&conn->framer, &frame, &got);
        if (err) return close_for(conn, tas_wire_err_name(err));
        if (!got) return;
        // Any inbound frame counts as liveness.
        conn->last_rx_us = now_us;
        if (frame.type != TAS_FRAME_JSON) continue;
        tas_msg_t msg;
        err = tas_msg_decode(frame.body, frame.len, &conn->arena, &msg);
        if (err) close_for(conn, tas_wire_err_name(err));
        else handle(conn, &msg, now_us);
        tas_arena_free(&conn->arena);
    }
}

void tas_conn_ended(tas_conn_t *conn) {
    close_for(conn, tas_framer_close(&conn->framer) ? "hub closed mid-frame" : "hub closed");
}

void tas_conn_tick(tas_conn_t *conn, uint64_t now_us) {
    if (now_us >= tas_conn_deadline(conn)) close_for(conn, "hub went silent");
}

uint64_t tas_conn_deadline(const tas_conn_t *conn) {
    return conn->last_rx_us + TAS_CONN_DEAD_US;
}

void tas_conn_event(tas_conn_t *conn, const char *cap, const char *name, uint64_t ts,
                    const uint8_t *data, size_t len) {
    if (conn->state != TAS_CONN_WELCOMED) return;
    // Its own arena: a command being answered may still hold the Connection's.
    tas_arena_t arena = {.limit = TAS_CONN_ARENA_LIMIT};
    tas_msg_t event = {.type = TAS_MSG_EVENT};
    tas_json_t *tree;
    if (tas_json_parse(data, len, &arena, &tree) == TAS_JSON_OK) {
        event.event.seq = conn->event_seq++;
        event.event.cap = tas_str(cap);
        event.event.name = tas_str(name);
        event.event.ts = ts;
        event.event.data = tree;
        send(conn, &event);
    }
    tas_arena_free(&arena);
}

const uint8_t *tas_conn_outbox(const tas_conn_t *conn, size_t *size) {
    *size = conn->out_len;
    return conn->out;
}

void tas_conn_sent(tas_conn_t *conn, size_t size) {
    memmove(conn->out, conn->out + size, conn->out_len - size);
    conn->out_len -= size;
}

void tas_conn_close(tas_conn_t *conn, const char *why) {
    tas_arena_free(&conn->arena);
    close_for(conn, why);
}
