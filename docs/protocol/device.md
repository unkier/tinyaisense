# Device↔hub protocol

Protocol major version: **1**. See ADR 0001 (Kinds with three primitives) and ADR 0002 (TCP control plus UDP streams).

Each device keeps one TCP connection to the hub, the **Connection**. It carries JSON control messages: the handshake, commands, replies, events and keepalive. Stream frames and a mirror of button events travel as UDP datagrams. Both sides set `TCP_NODELAY`.

## Ports and discovery

| Port | Protocol | Use |
|---|---|---|
| 8420 | TCP | device Connections |
| 8420 | UDP | device streams and event mirrors |
| 8421 | TCP | HTTP: `/agent` and `/mcp` |

- The hub advertises the mDNS service `_tinyaisense._tcp`. The SRV record carries the TCP port, and TXT carries `proto=1`, the protocol major. It advertises one IPv4 address: that of the interface its mDNS goes out on.
- The device uses the first hub it finds whose `proto` matches its own major.
- A static `hub_host:port`, set over the serial console, overrides mDNS.

## TCP framing

Every frame starts with a 4-byte header:

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `type` | u8 | frame type |
| 1 | `flags` | u8 | per-type flags |
| 2 | `len` | u16 LE | body length in bytes, at most 4096 |

Then `len` bytes of body follow.

| `type` | Body | MVP |
|---|---|---|
| 1 | one JSON message, UTF-8 | yes; `flags` is sent as 0 and ignored |
| 2 | payload chunk: `cmd_id u32 LE`, then bytes. `flags` bit 0 marks the last chunk | specced, not built |
| 3 | reserved for in-band stream frames (a future gateway or UDP-less networks) | no |

- A frame with `len` above 4096 is a protocol error (`frame_too_large`). The receiver closes the Connection as soon as it reads the header.
- Receivers skip frames of any type they don't handle. In the MVP that means types 2, 3 and anything unknown.
- **Payload chunks (type 2), for later Kinds such as a camera.** A `cmd` or `reply` that carries bulk bytes adds `"payload":{"size":N,"mime":"…"}`. The bytes follow as type-2 frames of at most 4 KB each, tagged with the command `id`. The command timeout covers the whole transfer. No MVP command carries a payload.

## JSON messages

Every type-1 frame holds exactly one JSON object with a string `type`.

| `type` | Direction | Fields, in canonical order |
|---|---|---|
| `hello` | device → hub | `token`: string or `null`; `udp_port`: u16, not 0; `manifest`: Manifest |
| `welcome` | hub → device | `name`: string; `conn_id`: u32, not 0; `udp_port`: u16, not 0; `token`?: string |
| `pending` | hub → device | none |
| `error` | hub → device | `code`: string; `message`: string; `protocol`: u32 |
| `cmd` | hub → device | `id`: u32; `cap`?: string; `name`: string; `args`: object |
| `reply` | device → hub | `id`: u32; `ok`: `true`; `result`: object |
| `reply` | device → hub | `id`: u32; `ok`: `false`; `error`: `{code: string, message: string}` |
| `event` | device → hub | `seq`: u32; `cap`: string; `name`: string; `ts`: u64; `data`: object |
| `ping` | hub → device | `t`: u64 |
| `pong` | device → hub | `t`: u64; `ts`: u64 |

`?` marks an optional field. `type` always comes first.

### Manifest

The device's self-description, sent in `hello` and fixed for the life of the Connection. To change it, the device reconnects.

| Field | Type | Meaning |
|---|---|---|
| `protocol` | u32 | the protocol major, `1` |
| `device_id` | string | the eFuse MAC as 12 lowercase hex digits |
| `board` | string | the Board, e.g. `m5stack-atom-echo` |
| `firmware` | string | the firmware version, from `git describe` |
| `capabilities` | array | `{id, kind, params?}` per capability |
| `exclusive` | array of arrays of strings | groups of capability ids of which at most one is active at a time; may be empty |

- A capability `id` matches `[a-z0-9-]+` and is unique within the device. By convention it's the Kind name (`button`, then `button-2`).
- `kind` is a string. `params` is an object whose shape the Kind contract defines, omitted when empty. For a known Kind, `params` that break the contract's `params` schema are a broken Manifest.
- Every id in `exclusive` names a capability of the same Manifest.
- A capability of a Kind the hub doesn't know is ignored with a warning. The rest of the device still works.

The Atom Echo Manifest:

```json
{"protocol":1,"device_id":"a1b2c3d4e5f6","board":"m5stack-atom-echo","firmware":"0.1.0-3-g1a2b3c4",
 "capabilities":[
  {"id":"mic","kind":"mic","params":{"formats":[{"codec":"pcm_s16le","rate":16000,"channels":1,"frame_ms":[10,20]}]}},
  {"id":"speaker","kind":"speaker","params":{"formats":[{"codec":"pcm_s16le","rate":16000,"channels":1,"frame_ms":[10,20]}]}},
  {"id":"button","kind":"button"},
  {"id":"led","kind":"led","params":{"count":1}}],
 "exclusive":[["mic","speaker"]]}
```

### JSON rules

**Receivers accept any valid JSON**, in any key order and with any whitespace, but reject with `bad_json`:
- invalid UTF-8, or a leading byte-order mark;
- invalid JSON, or a top-level value that isn't an object;
- a duplicate key in any object;
- `NaN`, `Infinity`, a number that overflows a double, and unpaired UTF-16 surrogate escapes;
- nesting deeper than 16 levels, counting the top-level object as level 1;
- an integer beyond ±(2^53 − 1), so that every decoder can hold any integer in a double. JSON `u64` fields such as `ts` therefore stay below 2^53, which the device clock reaches after 285 years.

**A known `type` with a missing or mistyped field** is `bad_message`. Integers must be JSON integers in range: `1.0` and `true` are not integers.

**Senders write canonical JSON.** The golden vectors rely on it for byte-identical round trips:
- no whitespace;
- message fields in the order of the tables above, optional fields omitted when absent;
- free-form objects (`args`, `result`, `data`, `params`) keep their key order;
- strings as raw UTF-8. Only `"`, `\` and U+0000–U+001F are escaped: `\b \f \n \r \t` by their short forms, the rest as `\u00xx` in lowercase hex. `/` is not escaped;
- integers in plain decimal, never in exponent form, however large. Beware `printf("%g")`, which cJSON uses. The protocol carries no fractional numbers. A receiver still accepts one inside a free-form object, but its re-encoding isn't pinned down, so no vector carries one.

## Handshake and Pairing

1. The device opens its UDP socket, connects TCP and sends `hello`. If no `hello` arrives within 5 s, the hub closes the Connection.
2. The hub answers with one of:

| Case | Answer |
|---|---|
| `manifest.protocol` isn't the hub's major | `error{code:"protocol_mismatch", protocol:<hub major>}`, then close |
| `token` is set but doesn't match the one stored for `device_id` | `error{code:"unauthorized"}`, then close |
| `token` is `null` | `pending`. The Connection stays open with pings until the owner approves; then `welcome` with `token` |
| `token` is valid | `welcome` without `token` |

3. **Nothing flows before `welcome`** except `ping` and `pong`. Any other known message before `welcome`, or a second `hello`, is a protocol error and closes the Connection.

**Token rules**
- The hub issues the token once, at Pairing. It is an opaque ASCII string of at most 64 characters, bound to `device_id`. The hub stores only its sha256.
- The device stores the token in NVS.
- On `unauthorized`, the device wipes its token and reconnects with `token: null`, so it shows up as pending again.
- A device without a token is always pending, even if it was paired before. Approving it replaces the stored token.
- With `serve --auto-approve`, the hub skips the pending step and answers `welcome` with a new `token` at once.

**Superseding:** a `hello` whose `device_id` already has a Connection supersedes it. The hub closes the old Connection first.

**`welcome` fields**
- `name` is the hub-owned device name (`[a-z0-9-]+`). The device never sends a name.
- `conn_id` is a random non-zero u32 that tags every datagram of this Connection.
- `udp_port` is the hub's UDP port.

## Keepalive and clock

- The hub sends `ping{t}` every 2 s, the first as soon as the `hello` is in, pending Connections included. `t` is the hub's monotonic clock in µs; the device treats it as opaque.
- The device answers at once with `pong{t, ts}`: `t` echoed, `ts` its own clock when it sends the pong.
- The hub keeps a min-RTT filter over the pongs to get the device-clock offset, and maps device `ts` values to hub time with it.
- Any inbound TCP frame counts as liveness. **After 6 s with no inbound frame, the Connection is dead** on either side, which closes it. The device never sends `ping`.

## Commands

A Command is `cmd{id, cap, name, args}`. The device answers every one with exactly one `reply` of the same `id`.

- `cap` is the capability id. It is omitted for device-level commands (`status`, `identify`, `diag`).
- The hub assigns `id` per Connection, starting at 1 and incrementing. After 2^32 − 1 it wraps to 1.
- Commands may be pipelined. The hub matches replies by `id` only; replies may arrive out of order across capabilities.
- `args` and `result` are objects whose shape the Kind contract defines. A command with nothing to say has `args: {}`, and a success with nothing to return has `result: {}`.
- **Timeouts are enforced on the hub only.** The timeout runs from sending the `cmd` to receiving its `reply`: 1000 ms, or the command's `timeout_ms` in its contract. When it expires the command fails with `timeout`, and a late reply is dropped. A reply with an unknown `id` is ignored.

**Error codes** in `reply.error.code`:

| Code | Raised by | Meaning |
|---|---|---|
| `unsupported` | device | unknown capability id, unknown command, or a capability of an unknown Kind |
| `invalid_args` | device | `args` fail the command's schema, or a device-specific check (e.g. an `index` past the LED count, or a format the capability doesn't advertise) |
| `busy` | device | an `exclusive` group peer is active, e.g. the Reflex holds the mic |
| `failed` | device | anything else; `message` says what |
| `timeout` | hub | no reply within the timeout |
| `disconnected` | hub | the Connection dropped with the command in flight |

## Events

An Event is `event{seq, cap, name, ts, data}`.

- `seq` counts every event of the Connection across all capabilities, starting at 0.
- `ts` is the device clock when the thing happened. For a button, it's the first edge.
- Events go out on TCP in `seq` order.
- **Event mirror:** button events are also sent as UDP datagrams of kind 2, twice, about 5 ms apart. The hub acts on the first copy to arrive, whichever path it came by, and drops later copies by `seq`. The hub accepts a mirror of any event.

## UDP datagrams

Each datagram is a 20-byte header followed by at most 1400 bytes of payload.

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `conn_id` | u32 | from `welcome` |
| 4 | `kind` | u8 | 1 = stream frame, 2 = event mirror |
| 5 | `stream` | u8 | stream id. An event mirror sends 0, and receivers ignore it |
| 6 | `flags` | u16 | bit 15 = encrypted (reserved). Other bits are sent as 0 and ignored |
| 8 | `seq` | u32 | stream: frame number from 0. Mirror: the event `seq` |
| 12 | `ts` | u64 | stream: see [`audio.md`](audio.md). Mirror: the event `ts` |
| 20 | payload | ≤1400 B | stream: one frame. Mirror: the event's JSON message, the same bytes as its TCP body |

**Addressing**
- The device binds its UDP socket before it sends `hello`, and sends the port as `udp_port`. The hub sends its own `udp_port` in `welcome`.
- Each side sends datagrams to the other's **TCP peer IP**, at the announced UDP port. No datagrams flow before `welcome`.
- A receiver drops and counts a datagram whose source IP isn't the Connection's TCP peer IP, or whose `conn_id` isn't the Connection's.

**Datagram checks.** The receiver drops and counts a datagram that:
- is shorter than the header (`short_datagram`) or carries more than 1400 bytes of payload (`datagram_too_large`);
- has `flags` bit 15 set (`encrypted`). The MVP has no keys to decrypt it;
- is a stream frame on stream 0 (`bad_stream`);
- is an event mirror whose payload isn't a valid `event`, or whose header `seq` or `ts` differs from the event's (`bad_json`, `bad_message`);
- is a stream frame that breaks the frame rules in [`audio.md`](audio.md).

A datagram of an unknown `kind` is ignored. **UDP junk never closes a Connection.**

## Connection end and reconnect

There is no resume: the Connection is the unit of state.

- **Hub side:** on drop, in-flight commands fail with `disconnected`, the Connection's streams close, and the device goes offline.
- **Device side:** on drop, every capability goes idle and the Reflex is disarmed. The device reconnects with backoff 0.5 → 1 → 2 → 5 s (the cap), each plus up to 20% random jitter, and resets the backoff after a `welcome`. Every Connection that ends, or fails to open, since the last `welcome` counts as one failure in a row, and waits the next step. After 3 failures in a row, and after every 3 more, it re-resolves mDNS before its next attempt.
- **Protocol errors close the Connection**, on either side: `frame_too_large`, `bad_json`, `bad_message`, a second `hello`, or a message before `welcome` that isn't allowed.

## Versioning

- The major version lives in `manifest.protocol` and in the mDNS TXT `proto`.
- These changes are additive and need no bump: new fields, message types, frame types, datagram kinds, Kinds, commands, events and args.
- Receivers ignore unknown fields, message types, frame types and datagram kinds.
- An unknown Kind in a Manifest is ignored with a warning, and the rest of the device works.
- An unknown command gets `unsupported`.
- A major mismatch gets an explicit `error{code:"protocol_mismatch"}` carrying the hub's major. The hub checks `manifest.protocol` before any other field of the `hello`, so a future major with a different `hello` shape still gets this answer.

## Reserved for TLS

None of this is in the MVP. The slots are reserved so that TLS lands without a major bump.
- mDNS TXT `tls=1`: the hub serves TLS on the device TCP port with a self-signed certificate.
- `cert_sha256` in `welcome`: the device pins it on first use.
- UDP: per-packet encryption keyed over the TCP Connection, marked by `flags` bit 15, with `conn_id` as the key id.

## Decode errors

The codecs report these codes. They're the `expect` values of the negative golden vectors.

| Code | Where | Meaning | Receiver action |
|---|---|---|---|
| `frame_too_large` | TCP | `len` above 4096 | close |
| `truncated` | TCP | the stream ended inside a frame | close |
| `bad_json` | TCP, mirror | see JSON rules | close (TCP), drop (UDP) |
| `bad_message` | TCP, mirror | a known message with a missing or mistyped field, or a broken Manifest | close (TCP), drop (UDP) |
| `protocol_mismatch` | TCP | `hello` with a different protocol major | answer `error{protocol_mismatch}`, close |
| `short_datagram` | UDP | fewer than 20 bytes | drop, count |
| `datagram_too_large` | UDP | payload above 1400 bytes | drop, count |
| `encrypted` | UDP | `flags` bit 15 set | drop, count |
| `bad_stream` | UDP | stream frame on stream 0 | drop, count |
| `partial_sample` | UDP | frame length isn't a whole number of samples | drop, count |
| `short_frame` | UDP | a non-final frame shorter than the frame size, or an empty frame | drop, count |
| `oversized_frame` | UDP | a frame longer than the frame size | drop, count |
