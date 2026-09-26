# Capabilities are concrete kinds with three primitives

Every capability declares a **Kind** from a fixed set: mic, speaker, button and led, with camera and screen later. The hub knows each kind's contract: its events, streams, commands and params. Every interaction is one of three primitives:
- **Event**: capability → hub.
- **Stream**: continuous, in either direction.
- **Command**: hub → capability, always replied to.

We rejected fully self-describing capabilities, where the manifest lists every command and its schema like an MCP tool list. They cost firmware RAM on a no-PSRAM ESP32 and strip away the semantics agents rely on.

We also rejected a Matter-style property/state model. Nothing in the MVP needs state sync, and a command's reply payload already covers request/response cases like a camera snapshot.

A new kind means a new contract in `docs/protocol/`. That is an additive change and does not bump the protocol major version.
