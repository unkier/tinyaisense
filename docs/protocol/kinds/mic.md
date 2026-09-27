# `mic` Kind

A Sensor that streams audio up to the hub. Streams, frames and loss handling are in [`audio.md`](../audio.md).

Contract data: [`contracts/mic.json`](../../../packages/wire/src/tinyaisense_wire/contracts/mic.json). It is normative for the args, result and event schemas, the `hub_only` flags and the timeouts.

## Params

- `formats`: the formats the mic can capture, each with a list of `frame_ms` values. See [Format](../audio.md#format).

## Events

### `started`

`{stream, format}`: the Reflex opened the mic on a device-range stream id (128–255). The hub has usually opened the stream already, from its first frame; this event only confirms.

### `stopped`

`{stream, reason:"reflex", last_seq, dropped}`: the button release closed a mic the Reflex had opened. `last_seq` is `null` if no frame went out.

## Commands

The hub drives the mic for `listen` and `stream.start`, so agents never send these directly.

### `start` (hub-only)

`{stream, format}` → `{format}`. Opens the mic on a hub-range stream id (1–127). The reply echoes the chosen format.
- `invalid_args`: the format isn't advertised.
- `busy`: the mic is already streaming, e.g. on a Reflex stream, or an `exclusive` peer is active. The hub stops the old stream first, pipelined.

### `stop` (hub-only)

`{}` → `{last_seq, dropped}`. Stops the mic's current stream, hub or Reflex, and replies once the final frame has gone out.
- `last_seq` is the stream's final `seq`, or `null` if no frame went out.
- `dropped` counts frames the device discarded on queue overflow. The hub can't tell those apart from WiFi loss by `seq` gaps.
- With no stream open, it replies `{last_seq: null, dropped: 0}`.
