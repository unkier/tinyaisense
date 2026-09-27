# `speaker` Kind

An Actuator that plays audio streamed down from the hub. Streams, pacing, the jitter buffer and its stats are in [`audio.md`](../audio.md).

Contract data: [`contracts/speaker.json`](../../../packages/wire/src/tinyaisense_wire/contracts/speaker.json). It is normative for the args, result and event schemas, the `hub_only` flags and the timeouts.

## Params

- `formats`: the formats the speaker can play, each with a list of `frame_ms` values. See [Format](../audio.md#format).

## Events

### `drained`

`{stream, played_ms, lost, underruns, underrun_ms}`: the speaker played through the `last_seq` of a `drain`, and the last sample has left the DMA.

### `stopped`

`{stream, reason:"reflex", played_ms, lost, underruns, underrun_ms}`: the Reflex cut the speaker. The event goes out right after `button.down` and before `mic.started`.

## Commands

The hub drives the speaker for `say` and `stream.start`. The volume is an owner setting, so agents never send these directly.

### `start` (hub-only)

`{stream, format, prefill_ms?}` → `{format}`. Opens the speaker on a hub-range stream id (1–127). Playback begins once `prefill_ms` (default 40) is buffered. The reply echoes the chosen format.
- `invalid_args`: the format isn't advertised.
- `busy`: the speaker is already playing, or an `exclusive` peer is active, e.g. the Reflex holds the mic. The human wins. To replace a stream, the hub sends `stop` then `start`, pipelined.

### `stop` (hub-only)

`{}` → `{played_ms, lost, underruns, underrun_ms}`. Cuts playback at once, with a 2 ms ramp. The stream id is dead and its later frames are dropped. With no stream open, all stats are 0.

### `drain` (hub-only)

`{last_seq}` → `{}`, acknowledged at once. No frames follow `last_seq`. The device plays through it without ever pausing, then emits `drained`. `last_seq: null` means the stream carried no frames, so `drained` follows at once. With no stream open, it replies `{}` and emits nothing.

### `set_volume` (hub-only)

`{level}` → `{}`. Software gain on a log curve, `level` 0–100: 0 is mute, 1 is about −40 dB and 100 is 0 dB.
- The hub stores the level per device and re-applies it after every `welcome`.
- The device boots at a Kconfig default and never persists the level.
