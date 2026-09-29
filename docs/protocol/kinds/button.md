# `button` Kind

A Sensor that reports presses. It also carries the device's Reflex (ADR 0003).

Contract data: [`contracts/button.json`](../../../packages/wire/src/tinyaisense_wire/contracts/button.json). It is normative for the args, result and event schemas, the `hub_only` flags and the timeouts.

## Params

None.

## Events

`down` and `up` are raw edges with `data: {}`. The event `ts` is the first edge; the device confirms it by re-reading the pin about 1 ms later, then ignores bounces for 20 ms. Both go out on TCP and are mirrored twice over UDP (see [Events](../device.md#events)).

### `down`

The button went down.

### `up`

The button went up.

**Hub-derived gestures.** The hub derives `click`, `double_click` and `long_press` from the raw edges and delivers them to agents. They never cross the device wire. It times them by the edges' `ts`, so network delay doesn't stretch a press. Every threshold is in the hub's `[timing]`.
- `click`: a press under 250 ms. It goes out as the button goes up, right after `up`. It never waits to see whether a double click follows.
- `double_click`: a second click whose press starts within 300 ms of the first one's release. It goes out right after the second `click`, so a double click is `click`, `click`, `double_click`. A third quick click starts over.
- `long_press`: a press held for 1 s. It goes out while the button is still down, and its release is no `click`.

While the Reflex is armed, a press under 250 ms is a `click`, meaning "stop talking", and there's no `long_press`.

## Commands

### `set_reflex` (hub-only)

`{armed:true, stop?, start?}` or `{armed:false}` → `{}`. Arms or disarms the Reflex. It's idempotent.
- `stop`: capability ids to cut on `down`, e.g. `["speaker"]`.
- `start`: `{cap, format}`, the mic to open on `down`, in that format.

The hub derives the targets from the Manifest and arms the Reflex on every device while a Session exists. Agents can only toggle `armed` per device, through the hub.

## Reflex

**On `down`, when armed**, the device acts without waiting for the hub:
1. It cuts every `stop` target with the DMA patch.
2. It runs the Board's `exclusive` switch hook.
3. It opens the `start` mic on a device-range stream id, in the armed format.

The events go out in this order: `button.down`, then `speaker.stopped{reason:"reflex", …}`, then `mic.started`.

**On `up`**, the device closes only a mic that the Reflex opened. That mic sends `mic.stopped{reason:"reflex"}` after `button.up`.

**Edge cases**
- If the mic is already streaming on a hub stream, nothing restarts, and only `down` and `up` go out. The hub decides what happens to that stream.
- If the hub stops a Reflex mic while the button is held (the 60 s cap, a `click`), the release sends only `up`.
- A button held across a reconnect is ignored until the next press.
- A Reflex acts only on its own device.
- On a drop, the device disarms the Reflex. The hub re-arms it after `welcome`.
