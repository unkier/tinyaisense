# One in-process, pluggable speech layer serves both MCP and the agent API

The hub's speech engines (VAD, end-of-turn, STT, TTS) sit in one **speech layer** above the device layer. MCP tools and the agent API are both clients of it; the agent API also keeps raw audio. Engines are Python classes loaded in-process by import path from the hub config, warmed up at startup, with inference in a thread pool. We chose this because lowest latency is the top priority, and no single engine wins across hardware, languages and local vs cloud, so engines must be swappable without touching either front-end.

## Considered Options

- **Out-of-process engines over the Wyoming protocol** (Home Assistant ecosystem, many ready servers). Rejected: an extra hop per chunk, and Wyoming STT is batch-only. A Wyoming adapter can still arrive later as just another plugin.
- **Speech only inside the MCP server.** Rejected: agent-API agents would each have to run their own STT/TTS just to get text.

## Consequences

- **The hub owns everything engine-agnostic:** clause chunking of text, resampling to the device format, mapping `played_ms` back to the text heard, per-VAD-segment STT during speech, and cloud fallback to local.
- **Engines declare** their languages, native sample rate and whether they emit partials. Roles: `vad`, `turn`, `stt`, `tts`, with per-language maps for `stt` and `tts`.
- **Agents see Voices and languages, never engine names.** Swapping an engine is a config change.
