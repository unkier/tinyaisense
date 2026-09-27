# Protocol docs

These docs, together with the Kind contract data they link (`packages/wire/src/tinyaisense_wire/contracts/*.json`), are the source of truth for the device↔hub protocol. The hub, the Simulator and the firmware all implement them. When the Simulator and the firmware disagree, fix these docs first, then whichever side is wrong.

- [`device.md`](device.md): transport, TCP framing, JSON messages, handshake, keepalive, commands, events, UDP datagrams, discovery and versioning.
- [`audio.md`](audio.md): the audio format, frames, stream ids, loss handling, the speaker jitter buffer and pacing.
- Kind contracts, one per Kind: [`mic`](kinds/mic.md), [`speaker`](kinds/speaker.md), [`button`](kinds/button.md), [`led`](kinds/led.md). Device-level commands and the Status table: [`device`](kinds/device.md).
- [`vectors/`](vectors/README.md): golden vectors. Both the Python and the C codec must pass every one.

The agent API is documented separately.

## Conventions

- Multi-byte integers on the wire are little-endian.
- `u8`, `u16`, `u32` and `u64` are unsigned integers of that width. In JSON they are plain integers in that range.
- The device clock is microseconds since boot, as a `u64`. Every device `ts` uses it.
- Capability ids and device names match `[a-z0-9-]+`.
