# The button Reflex runs on the device

Barge-in and push-to-talk run as a **Reflex** on the device, armed by the hub. On button down, the device cuts its own speaker and starts its own mic stream. On button up, it closes the mic. The hub and agent learn what happened from device events, sent after the fact. We chose this because lowest latency is the top priority, including under WiFi loss:
- **Speaker cut:** the speaker goes silent ~6 ms after the press. A hub-mediated cut measured 17 ms on clean WiFi.
- **Resilience:** the cut doesn't depend on the network or on the hub's Python event loop, which can stall during STT inference.

## Considered Options

- **Agent-driven** (the hub only forwards button events). Rejected: an MCP agent can't be interrupted during a blocking `say`, and every action costs an agent round trip.
- **Hub reflex** (the hub reacts to the button event and sends STOP and mic `start`). This keeps the firmware free of policy. We rejected it because the STOP travels over TCP, so a lost segment waits for a retransmit, and because hub stalls add directly to the delay.
- **Device cuts the speaker, hub opens the mic.** This saves most of the latency, but mic open still depends on the network. Opening the mic on the device is ~7 ms faster and also immune to loss.

## Consequences

- **Device-started streams:** the device starts a mic stream itself, using a stream id from a device-owned range. The rule "the hub assigns every stream id" is relaxed.
- **Hub state:** the hub's per-device state machine is driven by device events (speaker stopped with `reason: reflex`, mic started, mic stopped) as well as by its own commands.
- **Arming:** the hub arms the Reflex with a command on the button capability, naming the capabilities to stop and start by id and the mic format. The hub arms it while an agent session exists. An agent may disarm it to use the button for its own UI.
- **Generic trigger:** the trigger is defined generically, so a VAD speech-start on a future full-duplex device can arm the same Reflex.
