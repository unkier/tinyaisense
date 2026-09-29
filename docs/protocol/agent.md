# Agent API

Agents that need the instant Interrupt path, raw audio or streaming text talk to the hub over one WebSocket (ADR 0005). JSON-RPC 2.0 goes in text frames and audio in binary frames. MCP clients use `/mcp` instead, which sits on the same core and the same Session rules.

The Python SDK (`tinyaisense`) wraps this API; any WebSocket client can use it directly, a browser included.

## Transport

- `ws://<hub>:8421/agent`. Agents find the hub through their own config, the URL plus the agent token, not mDNS.
- One text frame holds one JSON-RPC 2.0 message. Batches aren't supported. One binary frame holds one audio frame (see [Binary frames](#binary-frames)).
- The hub declines compression (`permessage-deflate`): it would only add delay to small messages. A message may be at most 1 MiB.
- The hub sets `TCP_NODELAY` and keeps its socket write buffer small, so that a button Event never waits behind a backlog of audio.
- **The hub always reads eagerly.** It never stops reading an agent socket to push back, so the agent can send speaker audio as fast as it gets it, and a `stop` never queues behind unread audio.
- **Keepalive:** the hub sends a WebSocket ping every 2 s. If no frame at all arrives from the agent for 6 s (3 missed pings), the hub drops the link and its Session ends. Every WebSocket library answers pings by itself.
- A **link** is one such WebSocket. A link that hasn't sent `hello` within 5 s of connecting is closed.

## Messages

- **Requests** carry an `id` (a string or an integer) and a `method`. `params`, if given, is an object; omitting it is the same as `{}`.
- **Replies** carry the request's `id` and either a `result` object or an `error`. Requests may be pipelined, and replies may arrive in any order: match them by `id`.
- **Notifications** from the hub carry a `method` and a `params` object, and no `id`. The agent never replies to them. The hub ignores notifications from the agent.
- **Unknown parameters** in a request get `invalid_args`, so a hub that is too old for a new option fails clearly instead of ignoring it. The other way round, agents must ignore unknown fields in results and notifications, and notifications whose `method` they don't know: a newer hub may send them.
- **Timestamps:** every notification carries `t`, the hub's wall clock in epoch ms. For a device Event, `t` is when it happened on the device, mapped onto the hub clock through the Connection's clock sync. For everything else, it's when the hub sent it.

### Errors

A failed request gets a JSON-RPC error. Branch on `data.code`, a string; `message` is for humans; `data` may carry more fields.

```json
{"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"kitchen/speaker is busy: the human holds the mic","data":{"code":"busy"}}}
```

| `data.code` | Meaning |
|---|---|
| `unauthorized` | a bad agent token, or a request before `hello` |
| `unsupported` | an unknown method, a command the capability doesn't have, or a feature the hub isn't configured for (e.g. `say` with no TTS engine) |
| `invalid_args` | params that break the method's schema, an unknown param, invalid JSON, or an ambiguous bare Address (the message and `data.candidates` list the candidates) |
| `unknown_address` | no paired device or capability by that name |
| `busy` | the hub is held by another Session, or the capability is in use and the request can't take it over |
| `failed` | the device or the hub tried and failed |
| `timeout` | the device didn't reply in time |
| `disconnected` | the device dropped before it replied, or the stream or Session is gone |
| `overflow` | a buffer limit was hit, e.g. over 5 min of queued speaker audio |

The numeric `code` follows JSON-RPC: −32700 for invalid JSON (with `id` null), −32600 for a message that isn't a request, −32601 for an unknown method, −32602 for bad params, −32603 for a failure inside the hub (`failed`), and −32000 for everything else.

## Session

A **Session** is one agent's claim on the hub. The hub allows one at a time. The agent's streams, LED claims and Reflex settings belong to it and end with it.

### `hello`

The first request on a link must be `hello`:

| Param | Type | Meaning |
|---|---|---|
| `token` | string | the agent token from the hub config |
| `takeover` | bool, default `false` | end the Session that holds the hub, if any, and claim it |
| `utterances` | object, optional | the Session's default Utterance opt-in (see [Utterances](#utterances)) |

It replies `{session}`: the Session id, a string. An MCP link joins this Session by sending it in an `X-Hub-Session` header.

| Case | Error, then the hub closes the link |
|---|---|
| any other request first | `unauthorized` |
| a wrong token | `unauthorized` |
| another Session holds the hub, and no `takeover` | `busy`, with `data.holder` (`agent_api` or `mcp`) and `data.since` (epoch ms) |
| bad params | `invalid_args` |
| the hub is shutting down | `failed` |

A `hello` on a link that already holds a Session gets `invalid_args`.

### Rules

- **One at a time.** A `hello` while a Session exists gets `busy`.
- **Takeover wins.** With `takeover: true`, the hub first ends the old Session with reason `taken_over`, completely, and only then starts the new one and replies. An agent must not reconnect by itself after `taken_over`, or two agents would take the hub from each other forever; the SDK never does.
- **No resume.** A link that drops takes its Session with it. Reconnecting means a new Session and a fresh `devices`.
- **Several links, one Session.** An MCP link with `X-Hub-Session` acts within the Session. The agent-API link gets every notification in the Session, whichever link caused it; MCP links get only their tool results.
- **Arming.** While a Session exists, the hub arms the Reflex on every online device, and devices show the Status `agent`.

### Ending

A Session ends when:
- its agent-API link closes or goes silent for 6 s;
- another agent takes it over: `taken_over`;
- the hub shuts down: `shutdown`;
- an MCP-only Session sits idle: see the MCP server.

When the link is still up, the hub sends `session.ended{reason}` and then closes the link.

Ending a Session runs in a fixed order: stop its speaker streams, end its listens, release its LED claims, disarm the Reflex, then set every device's Status to `no_agent`. Its streams end with `disconnected`.

## Methods

| Method | Params | Result |
|---|---|---|
| `hello` | `token`, `takeover?`, `utterances?` | `{session}` |
| `devices` | none | `{devices: [Device]}` |
| `voices` | none | `{voices: [{id, lang, name}]}` |
| `command` | `address`, `name`, `args?` | the command's result object |
| `stream.start` | `address`, `rate?`, `prefill_ms?` | `{handle}` |
| `stream.stop` | `handle` | `{reason, stats}` |
| `stream.drain` | `handle` | `{reason, stats}` |
| `say` | `address`, `text`, `voice?`, `lang?`, `then_listen?`, `replace?` | a [say result](#say) |
| `say.open` | `address`, `voice?`, `lang?`, `then_listen?`, `replace?` | `{handle}` |
| `say.append` | `handle`, `text` | `{}` |
| `say.close` | `handle` | a say result |
| `listen` | `address`, `lang?`, `utterances?` | the `utterance.end` params |
| `reflex.set` | `device`, `armed` | `{}` |
| `led.release` | `device` | `{}` |
| `cancel` | `id` | `{}` |

An **Address** is `<device name>/<capability id>`, e.g. `kitchen/speaker`. A bare capability id (`speaker`) works when exactly one paired device has it, going by each device's last Manifest; if several have it, the request gets `invalid_args` with the candidate Addresses in `data.candidates`. An Address nothing has gets `unknown_address`. `device` params take a device name.

### `devices`

Every paired device, online or not, sorted by name. Pending devices are hidden: Pairing is the owner's business.

| Field | Meaning |
|---|---|
| `name` | the device name, the first half of its Addresses |
| `online` | whether it has a Connection now |
| `manifest` | its [Manifest](device.md#manifest), as it last sent it, including the `exclusive` groups as-is |

A capability's Kind fixes its commands, events and streams: see the Kind contracts. Hub-only commands are never callable by agents, so `led.set` and `led.off` are the only agent commands in the MVP. Capabilities of a Kind the hub doesn't know are listed, but the hub can't drive them.

### `command`

`command{address, name, args}` sends a Kind command to a capability and replies with its result.
- `args` defaults to `{}`, and is checked against the Kind contract first: `invalid_args`.
- Hub-only commands and unknown ones get `unsupported`, as does a capability of a Kind the hub doesn't know.
- A device that is offline gets `disconnected`.
- The device's own errors pass through: `unsupported`, `invalid_args`, `busy`, `failed`. The hub adds `timeout` and `disconnected`.
- Commands to one device run one at a time, in the order they came in: each waits for the reply to the one before. Devices never wait for each other.

**The LED claim.** The first agent `led` command on a device claims its LED: the device's Status display is suspended, except `listening` and `identify`. `led.release{device}` hands it back, as does the end of the Session; the hub sends the device the hub-only [`release`](kinds/led.md#release-hub-only). Releasing an LED the agent hasn't claimed does nothing. After the device reconnects, the hub re-applies the claim before `device.online`: per capability, the last command without an `index`, then the latest `set` of each `index` since. Only commands the device carried out count.

### `reflex.set`

`reflex.set{device, armed}` arms or disarms the Reflex on one device. The hub arms every device when the Session starts, keeps each device's setting for the Session, and re-applies it when the device reconnects. The Reflex targets come from the Manifest; agents can't change them.

While a device is armed, a button press cuts its speaker and opens its mic on the device itself, and a release closes that mic: see [Button](kinds/button.md). A disarmed button only reports Events.

### `cancel`

`cancel{id}` stops the request with that JSON-RPC `id`, if it's still running:
- a `say` or `say.close` cuts the speech; it replies with reason `stopped`;
- a `listen` ends with reason `stopped` and replies with what it has.

The cancelled request still gets its reply. Cancelling a request that has finished, or never existed, does nothing.

## Notifications

| Method | Params (besides `t`) |
|---|---|
| `device.online` | `device`, `manifest` |
| `device.offline` | `device` |
| `<kind>.<event>` | `address`, `data`: a device Event, e.g. `button.down` |
| `stream.started` | `handle`, `address`, `rate` |
| `stream.ended` | `handle`, `reason`, `stats` |
| `say.ended` | `address`, `text`, and the say result |
| `utterance.start` | `utterance_id`, `address`, `trigger`, `handle?` |
| `transcript.partial` | `utterance_id`, `text`, `lang` |
| `utterance.end` | `utterance_id`, `reason`, `transcript?`, `lang?`, `degraded?`, `stats` |
| `session.ended` | `reason` |

- **Presence.** `device.online` goes out when a paired device connects, with the Manifest of that Connection, once the hub has brought it up to date (volume, Status, Reflex, LED claim). `device.offline` goes out when the Connection drops. A device the owner renames drops and comes back, so agents see it go offline under the old name and online under the new one.
- **Device Events.** There are no subscriptions: the Session gets every Event from every device. The method is the Kind and the Event name, and `data` is the Event's data. Button Events are `button.down`, `button.up`, and the hub-derived `button.click`, `button.double_click` and `button.long_press` (no `long_press` while armed). Mic and speaker Events don't reach agents as such: they surface as `stream.*`, `say.*` and `utterance.*`.
- Events of a new Kind follow the same pattern, so a new Kind needs no API change.

### Ordering

- Per device, Events arrive in the device's order (its event `seq`).
- **Cause before effect:** `button.down`, then the cut `say` result or `stream.ended` with reason `reflex`, then `utterance.start`.
- Across devices, in the order the hub received them.
- A stream's binary frames come after its `stream.start` reply or `stream.started`, and before its `stream.ended`. An Utterance's frames come after its `utterance.start` and before its `utterance.end`.
- JSON messages never wait behind audio frames: the hub sends every pending JSON message before the next audio frame. The one exception is a stream's `stream.ended`, which waits for that stream's own last frames, so other messages may overtake it.

### The Interrupt contract

- **≤20 ms p99** from the button going down on the device to `button.down` written on the agent socket, on the LAN. Raw `button.down`/`up` skip every hold in the hub, including the per-device ordering wait.
- **The hub only delivers Events.** It never interrupts an agent's turn itself. An adapter next to the agent decides whether a press interrupts, e.g. the SDK's default policy: `button.down` on an armed device while a turn is running cancels the turn; an idle agent isn't interrupted; a `click` interrupts without starting a new turn.
- The Reflex has already cut the speaker on the device by then. The cut `say` result says what the human heard (`heard_text`), so the agent can tell its model where it was cut off.

## Streams

A **stream** carries raw audio between the agent and a capability. `stream.start{address, rate}` opens one on a mic (device → agent) or a speaker (agent → device):
- `rate` is 16000 (the default), 24000 or 48000. The hub resamples both ways, so e.g. OpenAI Realtime's 24 kHz passes straight through.
- `prefill_ms` (speaker only, `invalid_args` on a mic; default 40, at most 200) is how much audio the device buffers before it starts playing.
- It replies `{handle}` once the device has started. A u16 **handle** names the stream within the Session; the hub also announces every stream, including those it opens itself, with `stream.started`.

**Ending.** `stream.stop{handle}` ends a stream at once: a mic stream once its last frames are in, which takes at most 30 ms more. `stream.drain{handle}` ends a speaker stream after everything sent so far has played; a mic stream can't be drained (`unsupported`). Both reply with `{reason, stats}`, as does the `stream.ended` notification every stream gets:

| `reason` | Why |
|---|---|
| `drained` | a speaker stream played to its end |
| `stopped` | the agent stopped it, or the device failed its `drain` |
| `preempted` | another stream took the capability, or its `exclusive` peer |
| `reflex` | a button press cut the speaker |
| `disconnected` | the device dropped, or the Session ended |
| `overflow` | the agent sent more than 5 min ahead of playback |

- Speaker `stats`: `{played_ms, lost, underruns, underrun_ms}`. `played_ms` is the stream position reached: frames lost on the network count toward it, pauses don't. When a device drops, it can't report them: the hub estimates `played_ms` as the audio it sent, capped by the time since it started sending, and reports the rest as 0.
- Mic `stats`: `{frames, lost, dropped, skipped}`: frames delivered, zero-filled ones included; frames the hub zero-filled for network loss; frames the device dropped from its queue, which the hub zero-fills too; and frames the hub skipped because the agent read too slowly.

**Speaker audio from agents.** The agent sends as fast as it gets audio. The hub buffers up to 5 min per stream and paces it to the device; there are no progress Events. Frames on a handle that has ended are dropped silently.

**Raw mic streams** have no hub end conditions: no VAD, no time limits. A button press during one delivers only `button.down`/`up`: no Utterance, and a release doesn't end it.

### Binary frames

One binary WebSocket frame is one audio frame, all integers little-endian:

| Direction | Layout |
|---|---|
| hub → agent | `handle` u16, `index` u32, then 10 ms of PCM |
| agent → hub | `handle` u16, then PCM of any even length; the hub re-chunks it |

- PCM is signed 16-bit little-endian, mono, at the stream's `rate`.
- A frame shorter than its header (`truncated`), or whose PCM isn't a whole number of samples (`partial_sample`), is dropped. So is a frame on a handle that doesn't name an open speaker stream of the Session. The link stays up either way.
- **Mic timeline.** `index` counts 10 ms frames from 0 at the start of the stream. The hub zero-fills audio lost on the network, so the timeline has no holes. If the agent reads too slowly and more than 2 s of a stream's audio waits for its socket, the hub drops the oldest frames: those, and only those, show up as gaps in `index`, and are counted in `skipped`.

### Overlap and exclusive groups

- **On one speaker,** `say`s queue first in, first out and play back to back with no gap. A `say` or `say.open` with `replace: true` cuts in instead: what is playing and queued ends `preempted`. Every other overlap replaces too: a new stream ends the old one, `preempted`.
- **The Reflex cancels everything:** the playing `say` or stream ends `reflex`, and the queued `say`s end `reflex` with `played_ms: 0`.
- **Exclusive groups** (e.g. `[["mic","speaker"]]` on the half-duplex Atom Echo). An agent starting one member stops the active peer first, which ends `preempted`. But the human wins: while a button press holds the mic, starting the speaker gets `busy`.

## Speech

The hub turns text into speech and speech into text in its own speech layer. Agents pick a **Voice** by id from `voices` and never see engine names.

### `say`

`say{address, text, voice?, lang?, then_listen?, replace?}` speaks `text` on a speaker and replies when playback ends:

| Field | Meaning |
|---|---|
| `reason` | `drained`, `stopped`, `preempted`, `reflex` or `disconnected` |
| `played_ms` | how much of this `say` played |
| `interrupted` | whether it ended before playing to the end |
| `heard_text` | the text the human heard: the original text of the chunks that played in full. A chunk cut halfway is left out |
| `degraded` | present and `true` if the cloud speech engine failed and a local one stood in |
| `utterance_id` | with `then_listen`: the Utterance it opened |

- **Language:** an explicit `lang`, else the text's dominant script (Cyrillic → `ru`, anything else → `en`), else the hub default. `voice` defaults to the hub's Voice for that language. One Voice per `say`.
- **`then_listen`** (`true` or `{raw: true}`, plus listen params): the hub opens the mic as soon as the speaker drains, with no round trip to the agent. The Utterance arrives as notifications, so `say` doesn't wait for the human's answer. With `raw: true` the hub opens a raw mic stream instead, announced by `stream.started`.
- With no TTS engine, `say` gets `unsupported`.
- Every `say` also ends with a `say.ended` notification, sent just before its reply.

### Streaming text

`say.open` → `{handle}`, then any number of `say.append{handle, text}`, then `say.close{handle}` → the say result. The hub cuts the text into clauses as it arrives, so speech starts after the first clause, not after the whole reply. Once the say has ended, e.g. cut by the Reflex, `say.append` is accepted and ignored, and `say.close` replies at once.

### Utterances

An **Utterance** is one span of human speech captured from a mic, delivered as a unit however it started:

- `utterance.start{utterance_id, address, trigger, handle?}`. `trigger` is `listen`, `then_listen` or `button`. `handle` names the stream carrying its audio, if the agent opted in to audio.
- `transcript.partial{utterance_id, text, lang}`: one per speech segment, if the agent opted in to transcripts.
- `utterance.end{utterance_id, reason, transcript?, lang?, degraded?, stats}`. `stats` is `{duration_ms, lost}`.

**Opt-in.** `hello{utterances}` sets the Session default, and each `listen` can override it:

| Field | Default | Meaning |
|---|---|---|
| `transcript` | `true` | deliver partials and the final transcript |
| `audio` | `false` | deliver the audio as binary frames |
| `rate` | 16000 | the audio's rate: 16000, 24000 or 48000 |

With no STT engine, Utterances carry no transcript, and `listen` gets `unsupported`.

**End reasons:**

| `reason` | Why |
|---|---|
| `stopped` | the agent cancelled the `listen` |
| `end_of_turn` | the human finished speaking: silence plus the end-of-turn model |
| `no_speech` | 8 s without speech |
| `max_duration` | 60 s in all |
| `button` | the human released the button |
| `click` | a press under 250 ms, which means "stop talking": discard any audio already received |
| `disconnected` | the device dropped, or the Session ended |

**`listen{address, lang?, utterances?}`** opens the mic and replies with the `utterance.end` params, sent right after the notification.
- A button press during a `listen` hands it to the human: the stream carries on without a restart, and the release ends it with `button`. The audio from before the press is kept.
- **Push-to-talk:** a press on an armed device with no `listen` running always delivers an Utterance, with trigger `button`.
- With no Session, the Reflex is disarmed and a press only produces raw Events, which nobody receives.
