# `led` Kind

An Actuator of one or more RGB LEDs. Agents drive it with the generic `command`. While an agent holds an LED claim, the device's Status is suspended, except `listening` and `identify` (see [Status](device.md#status)).

**The claim.** A `set` or `off` claims the capability's LEDs. A claim starts with every LED off, then the command applies, so LEDs a `set` with an `index` doesn't name stay off. The claim lasts until `release` or the end of the Connection. The Status shows on one LED capability the Board picks, the first on the Atom Echo; on any other, no claim means off.

Contract data: [`contracts/led.json`](../../../packages/wire/src/tinyaisense_wire/contracts/led.json). It is normative for the args, result and event schemas, the `hub_only` flags and the timeouts.

## Params

- `count`: the number of LEDs, at least 1.

## Events

None.

## Commands

### `set`

`{color, brightness?, pattern?, period_ms?, index?}` → `{}`.
- `color`: `"#rrggbb"`, hex digits in either case.
- `brightness`: 0–100, default 100. The Board scales it by its brightness cap, so 100 is the brightest comfortable level.
- `pattern`: `solid`, `blink` or `pulse`, default `solid`.
- `period_ms`: 200–5000, default 1000. The period of `blink` and `pulse`.
- `index`: which LED, from 0. With no `index`, all LEDs are set. An `index` of `count` or more gets `invalid_args`.

### `off`

`{}` → `{}`. Turns every LED off.

### `release` (hub-only)

`{}` → `{}`. Ends the agent's claim on the LEDs: they show the Status again. The hub sends it for `led.release` and when the Session ends. A claim also ends with the Connection.
