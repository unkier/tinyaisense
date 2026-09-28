# Device-level commands

These commands address the device itself, so their `cmd` has no `cap`. Every one is hub-only: agents never see them, and `identify` and `diag` are reachable only from the owner CLI.

Contract data: [`contracts/device.json`](../../../packages/wire/src/tinyaisense_wire/contracts/device.json). It is normative for the args and result schemas, the `hub_only` flags and the timeouts.

## Commands

### `status` (hub-only)

`{state}` → `{}`. Sets the hub layer of the Status: `no_agent`, `agent` or `thinking`.
- The hub layer starts at `no_agent` on every `welcome`. Right after `welcome`, the hub sends `agent` if a Session exists and `no_agent` otherwise, then keeps it up to date: `agent` when a Session starts, `no_agent` when it ends.
- `thinking` is set when an Utterance is delivered to the agent. It clears on the next speaker start or listen on that device, or after 60 s.

### `identify` (hub-only)

`{s?}` → `{}`. Blinks white at 4 Hz for `s` seconds (1–300, default 5), so the owner can tell devices apart. It sits on top of every other Status.

### `diag` (hub-only)

`{}` → `{uptime_s, reset_reason, heap_free, heap_min, rssi, reconnects, stack_hwm}`.
- `reset_reason`: why the device last rebooted, e.g. `poweron`, `panic`, `task_wdt`, `brownout`.
- `heap_free` and `heap_min`: bytes free now, and the lowest since boot.
- `rssi`: the WiFi signal in dBm.
- `reconnects`: Connections lost since boot.
- `stack_hwm`: `{task: bytes}`, each task's stack high-water mark.

## Status

What the device shows about itself, the hub and the agent. The device picks the highest-priority state that applies. A Board renders the states however it can; this is the Atom Echo's LED.

| Priority | State | Layer | Look |
|---|---|---|---|
| 1 | `identify` | command | white, 4 Hz blink |
| 2 | `listening`: a mic stream is open | device | green, solid. Shows even through an agent claim |
| 3 | agent LED claim | agent | whatever the agent set |
| 4 | `offline`: no WiFi | device | red, 1 Hz blink |
| 5 | `connecting`: WiFi up, no hub yet | device | amber, 2 Hz blink |
| 6 | `pending` | device | magenta, 2 s pulse |
| 7 | `no_config`: no WiFi credentials | device | red, solid |
| 8 | `speaking`: a speaker stream is open | device | blue, solid |
| 9 | `thinking` | hub | cyan, 1 s pulse |
| 10 | `no_agent` | hub | amber, dim, solid |
| 11 | `agent` | hub | off |
