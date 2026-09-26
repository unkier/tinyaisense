# Prior art: ESP32 ↔ server realtime voice protocols

Research for [#2](https://github.com/unkier/tinyaisense/issues/2), which feeds [#8](https://github.com/unkier/tinyaisense/issues/8) (the device↔hub transport and session lifecycle).

Written 2026-09-26. Sources are the projects' own repos, read at the pinned commits listed at the bottom. Every link is pinned to one of those commits.

Terms follow `CONTEXT.md`:
- **device**: the ESP32
- **hub**: their "server", "HA" or "backend"
- **stream**: continuous audio
- **event**: a discrete signal

## Comparison

| | Transport | Mic → server | Server → speaker | Framing | Session / control | Auth | Duplex / barge-in | Classic ESP32, no PSRAM? |
|---|---|---|---|---|---|---|---|---|
| **xiaozhi-esp32 (WS)** | 1 WebSocket; the device dials out | Opus 16 kHz mono, **60 ms**, complexity 0, DTX, VBR | Opus, rate and duration taken from the server hello (default 24 kHz / 60 ms) | Text frame = JSON. Binary frame = Opus, raw (v1), 16 B header (v2) or 4 B header (v3) | `hello` (audio_params, features), `listen start/stop/detect` (auto/manual/realtime), `tts start/stop/sentence_start`, `stt`, `abort`, `mcp` (JSON-RPC), no app ping | Bearer token, Device-Id and Client-Id headers. Config comes from a cloud OTA/activation call | Half duplex unless AEC is present. `abort` on button or wake word. Realtime (full-duplex) mode needs AEC | Same chip is supported (`atommatrix-echo-base`), but **not the Atom Echo board**. No AFE, AEC, VAD or wake word without PSRAM |
| **xiaozhi-esp32 (MQTT+UDP)** | MQTT (TLS, 8883) for control; UDP per session for audio | same | same | UDP: 16 B header `type,flags,len,ssrc,ts,seq`, which doubles as the AES-128-CTR IV | Same JSON over MQTT. The hello reply hands out the UDP host, key and nonce. `goodbye` | MQTT user/password plus a per-session AES key | same | same |
| **ESPHome `voice_assistant` + HA** | 1 TCP connection (native API, port 6053); **the hub dials the device**. Legacy UDP audio is being deprecated | **Raw PCM** s16le 16 kHz mono, **32 ms** (1024 B) per message | (a) a URL the device fetches over HTTP (WAV/MP3/FLAC/Opus), **used by the Atom Echo config**; or (b) raw PCM 512-sample chunks over the API, paced about 384 ms ahead | `0x00, varint len, varint type, protobuf`. Noise variant: `0x01, u16 len, ciphertext` | Hello/Ping/Disconnect. Device sends `VoiceAssistantRequest{start, flags, conversation_id}`; HA sends `Response{port}` and a stream of pipeline events. `Audio{data, end}`. Announce, timers, feature bitmask | Noise `NNpsk0_25519_ChaChaPoly_SHA256`, 32 B PSK (password auth removed) | Half duplex. Barge-in only on Voice PE (ESP32-S3 + XMOS AEC + stop word) | **Yes: an official Atom Echo config** with on-device wake word |
| **Wyoming** | TCP, unix socket or stdio; **the hub dials the satellite**; zeroconf `_wyoming._tcp` | Raw PCM, rate/width/channels declared per chunk. Satellite default is 1024 samples (64 ms) | Raw PCM (HA sends 22.05 kHz, 2048 B chunks, unpaced) | JSON header line (`type`, `data_length`, `payload_length`), then optional data JSON and binary payload | `describe/info`, `run-satellite/pause-satellite`, `run-pipeline`, `audio-start/chunk/stop`, `transcript`, `synthesize`, `played`, `ping/pong`, `error`, timers | **None** ("trusted network") | No AEC or barge-in logic. The mic can keep streaming during TTS | No official ESP32 implementation. wyoming-satellite is **deprecated** in favour of the ESPHome protocol |
| **Espressif esp-webrtc / esp_peer** | WebRTC (ICE, DTLS-SRTP, SCTP data channel). Signalling is BYO (AppRTC, WHIP, Janus, OpenAI HTTP, or ESP-as-server) | Opus or G.711, 16 kHz mono (openai_demo) | same | RTP/SRTP; data channel for control | Whatever the app puts on the data channel (e.g. OpenAI Realtime JSON events) | App-defined (e.g. Bearer on the SDP POST) | Full duplex with device AEC (`esp_capture` AEC source) on S3 | The library has an esp32 build and claims "<60 KB RAM without PSRAM", but that demo runs on an S3. **No voice demo on a classic ESP32.** Closed-source core |
| **LiveKit ESP32 SDK** | WebRTC via esp_peer: 2 PeerConnections, plus a protobuf-over-WS signalling socket | Opus 16 kHz mono, 20 ms | same | RTP; `_reliable` and `_lossy` data channels | LiveKit signal protocol, RPC, data streams | JWT (Bearer) | Device AEC plus server-side turn detection and interruption (LiveKit Agents) | **No**: S3/P4 only, PSRAM in every config. Developer preview |
| **Pipecat ESP32** | WebRTC via libpeer; one HTTP POST of the SDP offer to SmallWebRTC (aiortc); host candidates only | Opus 16 kHz, 20 ms, 30 kbps, complexity 0 | Opus | RTP; data channel `rtvi-ai` | RTVI JSON (the device handles only bot-started/stopped-speaking and tts-text) | None (TLS verification off) | Half duplex: the mic is zeroed while the bot speaks. Barge-in is server-side only. AtomS3R port pre-buffers **1 s** | **No**: S3 + PSRAM. Reboots on disconnect |

**Latency.** None of the seven publish a measured mouth→server or server→speaker figure. The only latency-relevant facts are the buffer and frame sizes quoted in the table and in the notes below.

## Per-project notes

### xiaozhi-esp32 (78/xiaozhi-esp32)

**Hello exchange.** The device sends:
```json
{"type":"hello","version":1,"features":{"mcp":true},"transport":"websocket","audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}
```
The server replies with `session_id` and its own `audio_params`, and must do so within 10 s.
- [websocket.md](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/docs/websocket.md#L24-L47)
- [websocket_protocol.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/protocols/websocket_protocol.cc#L182-L255)

**Binary headers** ([protocol.h](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/protocols/protocol.h#L19-L33)):
- `BinaryProtocol2`: `u16 version, u16 type, u32 reserved, u32 timestamp, u32 payload_size`
- `BinaryProtocol3`: `u8 type, u8 reserved, u16 payload_size`
- The RX path ignores `type`.

**Codec**
- 60 ms Opus frames, complexity 0, DTX on ([audio_service.h](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/audio/audio_service.h#L40-L80)).
- There is a fixed 120 ms warm-up each time the mic opens ([audio_service.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/audio/audio_service.cc#L276-L278)).
- The WebSocket connects lazily on the first button press ([websocket_protocol.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/protocols/websocket_protocol.cc#L79-L106)).
- Taken together, these would eat most of our 150 ms mic budget.

**Downlink gating.** Server audio is played **only in the Speaking state**, so `tts start` must come first ([application.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/application.cc#L554-L558)). Outside realtime mode the mic is off while speaking ([application.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/application.cc#L1046-L1055)).

**MCP**
- The **device is the MCP server** and the cloud backend is the client. JSON-RPC 2.0 is tunnelled as `{"type":"mcp","payload":…}` on the same session.
- It implements `initialize`, `tools/list` (paged at 8 KB) and `tools/call`, with tools like `self.audio_speaker.set_volume`.
- Sources: [mcp-protocol.md](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/docs/mcp-protocol.md#L41-L189), [mcp_server.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/mcp_server.cc#L417-L626)

**UDP crypto** ([mqtt_protocol.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/protocols/mqtt_protocol.cc#L204-L369))
- AES-CTR with no MAC.
- The same key and nonce template are used in both directions.
- The IV is built from the header, which *our reading* suggests allows keystream reuse.
- Do not copy it.

**Our chip without PSRAM**
- AFE, AEC, VAD and wake word are all Kconfig-gated off ([Kconfig](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/Kconfig.projbuild#L881-L959)). What remains is button-only, half duplex, with server-side end-of-utterance.
- Opus encode still fits: the `opus_codec` task has a 24 KB stack ([audio_service.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/audio/audio_service.cc#L144-L170)).
- No board targets the Atom Echo's PDM mic + NS4168 amp on ESP32. The closest board is [atommatrix-echo-base](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/boards/m5stack/atommatrix-echo-base/config.h#L8-L23), which uses an ES8311 codec.

**Semantics are cloud-LLM-shaped.** The protocol assumes a server that does STT, LLM and TTS: `stt`, `llm emotion`, `tts sentence_start`, and OTA activation against `api.tenclass.net` ([application.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/application.cc#L579-L723), [ota.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/ota.cc#L127-L246)).

### ESPHome `voice_assistant` + Home Assistant Assist satellite

**Frames**
- Plaintext: `0x00, varint len, varint type, protobuf` ([api.proto](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/api/api.proto#L84-L100)).
- Noise: `0x01, u16be len, ciphertext` over `Noise_NNpsk0_25519_ChaChaPoly_SHA256` ([noise_handshake.h](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/noise/noise_handshake.h#L13), [api_frame_helper_noise.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/api/api_frame_helper_noise.cpp#L420-L470)).
- TCP_NODELAY is set ([api_frame_helper.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/api/api_frame_helper.cpp#L204-L208)).

**Voice messages.** `VoiceAssistantRequest` (90), `Response` (91), `EventResponse` (92, with 16 event types), `Audio` (106, `bytes data, bool end, bytes data2`), plus Announce, Timer and Configuration ([api.proto](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/api/api.proto#L2051-L2221)).

**Mic stream**
- 32 ms raw PCM chunks from a 512 ms ring buffer ([voice_assistant.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.cpp#L21-L28)).
- Audio over the API is the current path; UDP is commented "will eventually be deprecated" ([voice_assistant.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.cpp#L380-L404)).

**Streamed TTS**
- HA paces about 384 ms ahead of a 512 ms device buffer ([HA assist_satellite.py](https://github.com/home-assistant/core/blob/d60a9257c99eb5e291c8704d88f65deaad821a85/homeassistant/components/esphome/assist_satellite.py#L705-L779)).
- The device waits for more than 4 KiB (about 128 ms) before playing ([voice_assistant.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.cpp#L441-L497)).

**Mic start is device-initiated.** The device sends `VoiceAssistantRequest{start=true}`. HA can only prompt it indirectly, through `AnnounceRequest{start_conversation}` or `continue_conversation` ([voice_assistant.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.cpp#L339-L376)).

**Single subscriber.** Only one subscriber may attach to the voice assistant ([voice_assistant.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.cpp#L563-L576)).

**Keepalive and reconnect**
- Device pings after 60 s idle and drops the connection at 150 s ([api_connection.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/api/api_connection.cpp#L354-L375)).
- Client keepalive is 20 s, and reconnect backoff caps at 60 s ([aioesphomeapi](https://github.com/esphome/aioesphomeapi/blob/26451b45fff576283826fd326845e5108f8ea296/aioesphomeapi/reconnect_logic.py#L60-L62)).

**Feature bitmask** in device info: VOICE_ASSISTANT, SPEAKER, API_AUDIO, TIMERS, ANNOUNCE, START_CONVERSATION, MULTI_CHANNEL_AUDIO ([voice_assistant.h](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.h#L37-L45)).

**Atom Echo config** ([m5stack-atom-echo.yaml](https://github.com/esphome/wake-word-voice-assistants/blob/a159d9aae35a4d0123aa0bc1d2ceaa5357c6cbad/m5stack-atom-echo/m5stack-atom-echo.yaml#L11-L96))
- ESP-IDF, no PSRAM.
- PDM mic on GPIO23.
- I2S speaker on GPIO22, set to `channel: stereo` because "poor playback audio quality when using mono", with a 60 ms buffer.
- Media-player buffer shrunk to 6000 B.
- Wake word disabled during playback, so there is no barge-in.
- This is **proof that raw-PCM voice streaming over TCP fits this exact hardware**.

**Aside.** aioesphomeapi and HA now have a listener (port 6054) where the device dials the hub ([outgoing_connection.py](https://github.com/esphome/aioesphomeapi/blob/26451b45fff576283826fd326845e5108f8ea296/aioesphomeapi/outgoing_connection.py#L1-L29)). The firmware side was not found in esphome `dev`.

### Wyoming (rhasspy/wyoming, rhasspy/wyoming-satellite)

**Framing** ([README](https://github.com/rhasspy/wyoming/blob/fa5d98c1e8d3f1b00eadcf46df6f87f1d352ca3e/README.md#L3-L60), [event.py](https://github.com/rhasspy/wyoming/blob/fa5d98c1e8d3f1b00eadcf46df6f87f1d352ca3e/wyoming/event.py#L82-L138)):
```
{"type":"audio-chunk","version":"1.10.2","data_length":58,"payload_length":2048}\n
{"rate":16000,"width":2,"channels":1,"timestamp":0}<2048 B PCM>
```

**Audio format per chunk.** `audio-start`, `audio-chunk` and `audio-stop` carry the format on each chunk, which makes the audio self-describing ([audio.py](https://github.com/rhasspy/wyoming/blob/fa5d98c1e8d3f1b00eadcf46df6f87f1d352ca3e/wyoming/audio.py#L19-L138)).

**Discovery and auth**
- Zeroconf `_wyoming._tcp.local.` ([zeroconf.py](https://github.com/rhasspy/wyoming/blob/fa5d98c1e8d3f1b00eadcf46df6f87f1d352ca3e/wyoming/zeroconf.py#L39-L48)).
- No auth or encryption, by design ([README](https://github.com/rhasspy/wyoming/blob/fa5d98c1e8d3f1b00eadcf46df6f87f1d352ca3e/README.md#L15-L20)).

**Liveness**
- The satellite sends ping/pong with a 2 s delay and a 5 s timeout ([satellite.py](https://github.com/rhasspy/wyoming-satellite/blob/7a3ba2bc23fc6699adae4f5bbac4ce529fe2a623/wyoming_satellite/satellite.py#L191-L268)).
- HA uses a 5 s ping timeout and a 10 s reconnect ([HA wyoming](https://github.com/home-assistant/core/blob/d60a9257c99eb5e291c8704d88f65deaad821a85/homeassistant/components/wyoming/assist_satellite.py#L48-L56)).

**Deprecation.** wyoming-satellite is deprecated, "replaced by Linux Voice Assistant that uses the ESPHome protocol" ([README](https://github.com/rhasspy/wyoming-satellite/blob/7a3ba2bc23fc6699adae4f5bbac4ce529fe2a623/README.md#L1-L4)). There is no official ESP32 implementation.

### WebRTC stacks (esp_peer, LiveKit, Pipecat, OpenAI embedded SDK)

**esp_peer**
- It claims "Even on platforms **without PSRAM**, a minimal setup uses **< 60 KB** RAM", but the example cited for that runs on an ESP32-S3 ([README](https://github.com/espressif/esp-webrtc-solution/blob/557dd14b8dbc69d57f102c7455c16d636026dc2a/components/esp_peer/README.md#L180-L184)).
- Default buffers are a 100 kB jitter buffer and a 400 kB send pool ([esp_peer_default.h](https://github.com/espressif/esp-webrtc-solution/blob/557dd14b8dbc69d57f102c7455c16d636026dc2a/components/esp_peer/include/esp_peer_default.h#L20-L55)).
- The core is a prebuilt `.a` licensed "EXCLUSIVELY with Espressif Systems products" ([LICENSE](https://github.com/espressif/esp-webrtc-solution/blob/557dd14b8dbc69d57f102c7455c16d636026dc2a/components/esp_peer/LICENSE#L5-L13)).
- The **doorbell_local** solution makes the ESP its own signalling server. That is the only LAN-only pattern ([README](https://github.com/espressif/esp-webrtc-solution/blob/557dd14b8dbc69d57f102c7455c16d636026dc2a/solutions/doorbell_local/README.md#L6)).

**LiveKit**
- "Supported chips: ESP32-S3 and ESP32-P4" ([README](https://github.com/livekit/client-sdk-esp32/blob/fbf09edf27a504d82119f730a35bf00fa287f7c1/README.md#L25-L29)).
- Two PeerConnections, each with 100 kB data-channel caches ([peer.c](https://github.com/livekit/client-sdk-esp32/blob/fbf09edf27a504d82119f730a35bf00fa287f7c1/components/livekit/core/peer.c#L289-L325)).
- The agent drives device LEDs and sensors via RPC ([agent.py](https://github.com/livekit/client-sdk-esp32/blob/fbf09edf27a504d82119f730a35bf00fa287f7c1/components/livekit/examples/voice_agent/agent/agent.py#L30-L41)).

**Pipecat**
- The device POSTs the SDP to `http://<host>:7860/api/offer` ([http.cpp](https://github.com/pipecat-ai/pipecat-esp32/blob/e70e3b1f0576e502af9e390434e1a6e8a5cd0d2e/esp32-s3-box-3/src/http.cpp#L84-L131)).
- The server has to munge its SDP answer for the ESP (drop sha-384/512 fingerprints, keep only host candidates) ([pipecat utils.py](https://github.com/pipecat-ai/pipecat/blob/2967e1c09ad484076089bc88427d305fe8f79fa9/src/pipecat/runner/utils.py#L406-L459)).
- The mic is zeroed while the bot speaks ([media.cpp](https://github.com/pipecat-ai/pipecat-esp32/blob/e70e3b1f0576e502af9e390434e1a6e8a5cd0d2e/esp32-s3-box-3/src/media.cpp#L18-L27)).
- The AtomS3R port pre-buffers 1 s ([media.cpp](https://github.com/pipecat-ai/pipecat-esp32/blob/e70e3b1f0576e502af9e390434e1a6e8a5cd0d2e/esp32-m5stack-atoms3r/src/media.cpp#L28-L32)).
- CPU and PSRAM needs: `CONFIG_SPIRAM=y` ([sdkconfig](https://github.com/pipecat-ai/pipecat-esp32/blob/e70e3b1f0576e502af9e390434e1a6e8a5cd0d2e/esp32-s3-box-3/sdkconfig.defaults#L15-L25)).

**OpenAI embedded SDK**
- It is the ancestor of Pipecat's client. The code now lives on its `esp32` branch, while `main` points to Espressif's `openai_demo` ([README](https://github.com/openai/openai-realtime-embedded-sdk/blob/3abe5fb05aaa99a0af8f7d215bf1ab52cb471732/README.md#L7-L9)).
- It targets S3 + PSRAM only, with Opus at 8 kHz in 20 ms frames ([media.cpp](https://github.com/openai/openai-realtime-embedded-sdk/blob/464a9f0d126ac0e3de99d6da9bdf9e418905cad1/src/media.cpp#L7-L19)).

## Cross-cutting findings

1. **Only ESPHome has voice working on our exact board.** It uses raw PCM over one TCP connection. xiaozhi supports the same chip on a different board (ES8311 codec). No WebRTC stack has a classic-ESP32 voice demo.
2. **Uplink format splits two ways.** One camp sends raw PCM 16 kHz s16le mono (ESPHome, Wyoming): no codec cost, 256 kbit/s, trivial on a LAN. The other sends Opus 16 kHz (xiaozhi, WebRTC stacks), which saves bandwidth for cloud links. Opus encode *does* fit the PICO-D4: xiaozhi gives the `opus_codec` task a 24 KB stack.
3. **Frame sizes span 20 to 64 ms:** 20 (WebRTC), 32 (ESPHome), 60 (xiaozhi), 64 (Wyoming). Our 150 ms mic budget favours 20 to 32 ms.
4. **TCP audio is fine on a LAN, and the field is moving to it.** ESPHome is deprecating UDP audio in favour of audio inside the API stream. xiaozhi's UDP path needs its own crypto, and gets it wrong.
5. **Every stack multiplexes two things over one connection:** typed control messages (JSON or protobuf) and binary audio. Each also has:
   - a hello/manifest (xiaozhi `hello.features`, ESPHome feature bitmask, Wyoming `info`)
   - explicit stream start/stop
   - app-level ping/pong
6. **Nobody does on-device barge-in without AEC,** and AEC needs PSRAM or an external DSP (XMOS). On the Atom Echo every stack is half duplex: the mic is muted during playback, and interruption comes from a button or server-side `abort`.
7. **The protocols bake in an STT→LLM→TTS pipeline.** xiaozhi has `stt`, `llm` and `tts sentence_start`. ESPHome has `VoiceAssistantEvent` STT/INTENT/TTS stages. Our hub contains no LLM, and our devices expose generic capabilities, so these semantics do not map cleanly onto device ↔ hub.
8. **Connection direction differs.** xiaozhi and WebRTC clients dial out to the server. ESPHome and Wyoming devices are servers that the hub dials (ESPHome is adding device-dials-hub). With mDNS discovery of the hub, device-dials-hub matches xiaozhi.
9. **Auth ranges from none to strong:** none (Wyoming, Pipecat), bearer token (xiaozhi WS, LiveKit JWT), Noise PSK (ESPHome). Noise NNpsk0 is a proven way to add encryption later on this chip class without TLS certificates.

## Verdict: design our own thin protocol, borrowing parts

**Don't adopt a protocol wholesale.**
- **xiaozhi:**
  - Its session semantics assume a cloud LLM pipeline and a cloud activation server.
  - It has no Atom Echo board.
  - It uses 60 ms frames.
  - Its UDP crypto is unsound.
- **ESPHome native API:**
  - It is proven on our hardware, but its voice messages are shaped around HA's Assist pipeline.
  - The mic can't be opened from the hub side (the device initiates).
  - It is entity-oriented protobuf tied to ESPHome's firmware framework.
  - We'd be adopting HA's model instead of our capability/event/stream model.
- **Wyoming:** it has no ESP32 implementation and is being retired in favour of ESPHome's protocol.
- **WebRTC** (esp_peer / LiveKit / Pipecat):
  - It is unproven on a no-PSRAM ESP32.
  - Its core is closed-source.
  - It needs ICE/DTLS/SDP munging for no LAN benefit.
  - Its references pre-buffer up to 1 s.

**Borrow these pieces (input to #8):**

| Piece | Borrow from | Why |
|---|---|---|
| One persistent connection, device dials the hub (found via mDNS); JSON text control plus binary audio frames on the same socket | xiaozhi WS (text=JSON, binary=audio); ESPHome/Wyoming (single multiplexed TCP) | Simplest; a WebSocket gives text/binary framing and a `wss://` TLS path for free |
| Tiny binary audio header (type, stream id, seq, device timestamp) | xiaozhi `BinaryProtocol3` / UDP header | Lets the hub detect loss/jitter and align button events with audio |
| Raw PCM s16le 16 kHz mono by default, 20 to 32 ms frames; Opus as a negotiated option | ESPHome (32 ms raw PCM on the Atom Echo); xiaozhi hello `audio_params` negotiation | No codec RAM/CPU on the device; latency headroom; Opus can come later without breaking the protocol |
| Hello/manifest listing capabilities and audio formats | xiaozhi `hello.features`; ESPHome feature bitmask; Wyoming `describe`/`info` | Maps directly to device/capability; extensible to camera and screen |
| Explicit stream start/stop events carrying format (`audio-start{rate,width,channels}` … `audio-stop`) | Wyoming | Self-describing streams; agent-requested mic fits naturally |
| Hub-paced speaker stream plus a small device pre-buffer (≈60 to 130 ms), and a "played"/drained acknowledgement | ESPHome raw TTS path (384 ms ahead, 128 ms pre-buffer, `AnnounceFinished`); Wyoming `played` | Bounded device RAM; meets the 200 ms start target if the pre-buffer stays small |
| App-level ping/pong with a short timeout, plus reconnect with capped backoff | ESPHome (60 s / 150 s, 60 s backoff cap); Wyoming (5 s pong timeout) | TCP alone doesn't surface a dead WiFi link quickly |
| Token auth now; Noise NNpsk0 or `wss://` later | xiaozhi Bearer header; ESPHome Noise PSK | Meets the MVP "per-device token", with a known path to encryption |
| Half duplex by default: mic muted while the speaker plays; button or hub `abort` interrupts playback | Every stack on no-AEC hardware | No AEC is available on the Atom Echo |

**Don't borrow:**
- xiaozhi's MCP-on-device. Our hub is the MCP server towards agents, and devices describe capabilities in the manifest, so tunnelling JSON-RPC to the device is unnecessary.
- xiaozhi's AES-CTR UDP scheme.
- The `stt`/`llm`/`tts` pipeline semantics.

## Sources (commits read)

| Repo | Commit |
|---|---|
| [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) | `8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c` |
| [esphome/esphome](https://github.com/esphome/esphome) (dev) | `a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1` |
| [esphome/aioesphomeapi](https://github.com/esphome/aioesphomeapi) | `26451b45fff576283826fd326845e5108f8ea296` |
| [esphome/wake-word-voice-assistants](https://github.com/esphome/wake-word-voice-assistants) | `a159d9aae35a4d0123aa0bc1d2ceaa5357c6cbad` |
| [esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe) | `d4e6fa43d6a1a7d342b7d4d403567e06516719de` |
| [home-assistant/core](https://github.com/home-assistant/core) | `d60a9257c99eb5e291c8704d88f65deaad821a85` |
| [rhasspy/wyoming](https://github.com/rhasspy/wyoming) (v1.10.2) | `fa5d98c1e8d3f1b00eadcf46df6f87f1d352ca3e` |
| [rhasspy/wyoming-satellite](https://github.com/rhasspy/wyoming-satellite) | `7a3ba2bc23fc6699adae4f5bbac4ce529fe2a623` |
| [espressif/esp-webrtc-solution](https://github.com/espressif/esp-webrtc-solution) | `557dd14b8dbc69d57f102c7455c16d636026dc2a` |
| [livekit/client-sdk-esp32](https://github.com/livekit/client-sdk-esp32) (v0.3.11) | `fbf09edf27a504d82119f730a35bf00fa287f7c1` |
| [pipecat-ai/pipecat-esp32](https://github.com/pipecat-ai/pipecat-esp32) | `e70e3b1f0576e502af9e390434e1a6e8a5cd0d2e` |
| [pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat) | `2967e1c09ad484076089bc88427d305fe8f79fa9` |
| [openai/openai-realtime-embedded-sdk](https://github.com/openai/openai-realtime-embedded-sdk) | main `3abe5fb05aaa99a0af8f7d215bf1ab52cb471732`, `esp32` branch `464a9f0d126ac0e3de99d6da9bdf9e418905cad1` |
