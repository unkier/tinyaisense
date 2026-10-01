# tinyaisense

## Running the hub

```sh
uv sync --all-packages --extra local  # with the local speech engines
uv run tinyaisense-hub models fetch   # their models (~890 MB) into ~/.local/share/tinyaisense/models
uv run tinyaisense-hub init      # writes ~/.config/tinyaisense/hub.toml with a random agent token
uv run tinyaisense-hub serve     # devices on port 8420, TCP and UDP; --auto-approve skips Pairing
uv run tinyaisense-hub devices   # paired and pending devices, online or offline, and when last seen
uv run tinyaisense-hub approve a1b2c3 as kitchen
uv run tinyaisense-hub rename kitchen den
uv run tinyaisense-hub identify den 10  # blinks it white for 10 s (default 5), to tell it apart
uv run tinyaisense-hub diag den      # uptime, reset reason, heap, RSSI, reconnects, stack high-water marks
uv run tinyaisense-hub volume den 60 # 0 (mute) to 100; the hub re-applies it on every reconnect
uv run tinyaisense-hub stats         # the histogram of button press to agent socket, over the last 10,000 edges
uv run tinyaisense-hub forget den
```

The config `init` writes speaks and listens with the local engines, on the CPU: Silero VAD v6, Smart Turn v3.2 for the end of the turn, Parakeet TDT 0.6B v3 for English and Russian speech to text, and for text to speech Piper `en_US-lessac` in English and Silero v5 in Russian. Its default Voices are `lessac` and `xenia`, and text written in Cyrillic is spoken in Russian. `models fetch` downloads the models they run from pinned URLs and checks each file's sha256; the hub refuses to start while any is missing. The `[local]` extra takes torch, for Silero, from PyTorch's CPU-only index. Silero's TTS models are licensed CC BY-NC 4.0, for non-commercial use only.

A cloud engine can serve a language too, always with a local engine to stand in for it: the `[polza]` extra has polza.ai's TTS and STT, which take the API key from env `POLZA_API_KEY`. The config names the two together, e.g. `ru = ["polza_tts", "silero_tts"]` under `[speech.tts]`. When the cloud engine fails, or gives nothing within 1 s (`cloud_first_byte_ms`), the local one speaks or hears in its place, and the agent gets `degraded: true`. After 3 failures in a row, the cloud engine is skipped for 60 s (`cloud_skip_ms`).

The hub advertises itself over mDNS as `_tinyaisense._tcp`, so devices find it without an address; `serve --no-mdns` turns that off. Every timeout, such as the 2 s ping and the 6 s dead Connection, lives in the config's `[timing]`.

The owner commands reach the running hub over a 0600 Unix socket in `$XDG_RUNTIME_DIR/tinyaisense/`, and no other way. Paired devices, with their volumes, live in `$XDG_STATE_HOME/tinyaisense/state.json`.

`serve` logs JSON lines to stderr, each with `t`, `level`, `logger` and `msg`. Some carry more fields, for `jq`:

- every stream's end has `"type": "stream.ended"`, with its `stats`, `counters`, `gaps` and, for a mic, `mouth_to_hub_ms` (`p50`, `p99` and `count`), the time from each frame's first sample to the hub;
- `"type": "loop.lag"` warns that the hub's loop ran a timer over 5 ms late (`loop_lag_ms`), at most once a second, with the `count` and `max_ms` since the last warning.

To debug a bad conversation, set `record_dir` under `[debug]`. The hub then keeps a dir per Session there, holding a 16 kHz WAV per stream (what the mic heard, or what the speaker was sent) and `trace.jsonl`. The trace records the agent's calls and their replies, every notification the agent got, and when each stream started and ended. On SIGTERM, the hub ends the Session (`shutdown`) before it closes the device Connections.

## Agents over MCP

MCP clients reach the hub at `http://<hub>:8421/mcp` over Streamable HTTP, with the agent token as `Authorization: Bearer <token>`. For agents on other machines, add the name they reach the hub by to `allowed_hosts`.

`tinyaisense mcp`, in the SDK's `[mcp]` extra, serves the same tools over stdio. It holds a Session over the agent API, so with `--channel` it pushes push-to-talk Utterances, button gestures and the results of calls that returned `pending` into Claude Code, as channel events that wake it while it's idle:

```sh
pip install 'tinyaisense[mcp]'
claude mcp add tinyaisense -e TINYAISENSE_TOKEN=<agent_token> -- tinyaisense mcp --hub http://hub.local:8421 --channel
claude --dangerously-load-development-channels server:tinyaisense
```

When another agent holds the hub, the shim exits and says who; `--takeover` takes the hub from it instead. The shim exits too once its Session ends, e.g. taken over.

Tools such as `say` block for up to 50 s before they return `pending`, so give MCP clients a read timeout of at least 60 s: in the OpenAI Agents SDK, `client_session_timeout_seconds=60`.

## Agents in Python

The SDK, `tinyaisense`, is an asyncio client for the agent API at `ws://<hub>:8421/agent`, the one way to the instant Interrupt path, raw audio and streaming text. Its `[agents]` extra runs an existing [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) agent by voice:

```python
import tinyaisense
from agents import Agent
from tinyaisense.openai_agents import HubMcpServer, VoiceAgent

async with (
    tinyaisense.connect("ws://hub.local:8421/agent", token) as hub,
    HubMcpServer(hub) as tools,  # the hub's MCP tools, in the same Session
):
    agent = Agent(name="butler", instructions="You run the house.", mcp_servers=[tools])
    await VoiceAgent(hub, agent).run()  # until the Session ends
```

- What the human says by push-to-talk is the input of a turn, `Runner.run_streamed`, and the reply is spoken as it streams, through `say.open/append/close` on the same device: one `say` for each model response, so that the `say`s its tools make follow it.
- A press interrupts the running turn: `cancel("immediate")`. The Reflex has already cut the speaker, and the next user message starts with *you were cut off; the human heard: '…'*, of the reply and the `say`s of its tools. The heard text also goes back into the conversation, `session.add_items`, since the cancelled run keeps none of the reply. A click interrupts too, and starts no turn; an idle agent isn't interrupted, and a disarmed device never interrupts. `policy=` swaps in another rule, `(event, armed) -> bool`.
- The conversation lives in `session=`, an Agents SDK session, in memory by default. `on_turn=` gets each `Turn` once it has ended: its input, how it was spoken, and its run.
- `HubMcpServer` joins the Session with `X-Hub-Session`, with a 60 s timeout, and `VoiceAgent` adds its server instructions to the agent's.

## Development

The Python packages live in one uv workspace under `packages/`:

| Package | Python | Path |
|---|---|---|
| `tinyaisense-wire` | ≥3.11 | `packages/wire` |
| `tinyaisense-hub` | ≥3.14 | `packages/hub` |
| `tinyaisense-sim` | ≥3.14 | `packages/sim` |
| `tinyaisense` (SDK) | ≥3.11 | `packages/sdk` |

```sh
uv sync --all-packages --extra local --extra polza --extra bench
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pyright --pythonversion 3.11 packages/wire packages/sdk docs/protocol/vectors
uv run pytest
```

The device↔hub protocol is specified in [`docs/protocol/`](docs/protocol/README.md). Its golden vectors are generated by `docs/protocol/vectors/generate.py`.

Hub integration tests run the real hub in-process on ephemeral ports and drive it with simulated devices from the `simulator` pytest fixture, which `tinyaisense-sim` exports. A simulated device finds the hub over mDNS unless given its address, and `device.faults` breaks its Connection on demand: `rst()`, `close()`, `vanish()` and `stall_reads`. `device.faults.udp_down` and `udp_up` fault the datagrams from and to the hub, seeded from the Simulator's `seed` and live: Bernoulli `loss`, Gilbert–Elliott `bursts`, `delay_ms`, `jitter_ms`, `duplicate`, `drop(stream, seqs)` and `blackout(ms)`; `mic_tx_stall` holds the mic's frames in its 200 ms queue. `device.speaker.captured` records what the speaker played, with when each frame was heard. `device.mic.feed(pcm)` gives the mic something to hear, in real time or with `burst=True` at once, from `tinyaisense_sim.sources`: `silence`, `tone`, `wav` files and raw `pcm`, resampled to 16 kHz mono. `device.button` presses and clicks; while the hub has armed the Reflex (`device.armed`), a press cuts the speaker and opens the mic, as firmware does, and `await device.talk(pcm)` presses, lets the mic hear `pcm` in real time, then releases. The mDNS tests need multicast on some interface, and each uses a service type of its own so that it never finds another hub on the LAN.

Most speech tests use a fake speech plugin, whose TTS speaks text as tones that its STT decodes. The local engines are tested on real English and Russian speech from `packages/hub/tests/audio/`, and what the TTS engines say, Parakeet hears back; those tests are skipped unless the `[local]` extra is installed and `models fetch` has run. The polza.ai engines run against a fake polza.ai, `packages/hub/tests/fake_polza.py`, which speaks and hears the fake plugin's tones. The `typings/` stubs give pyright the parts of onnxruntime, sherpa-onnx and soxr that the hub uses, since none of them ships types.
### The speech bench

`bench speech` times the latency gates of the model tier on the hub the config makes, run as `serve` runs it, in a process of its own, with simulated Atom Echos for devices and the SDK for the agent. It needs the `[bench]` extra.

```sh
uv run tinyaisense-hub bench speech          # STT and TTS: 3 runs of each
uv run tinyaisense-hub bench speech --load   # and 1000 presses a run, while STT and TTS are busy
```

- STT: push-to-talk on 30 English clips from LibriSpeech test-clean and 30 Russian ones from Common Voice 17.0, from the release, right after the last speech, to the transcript. p95 ≤150 ms; the word error rate is reported, not gated. The clips are pinned by sha256 in `packages/hub/src/tinyaisense_hub/bench/corpus.py`, and fetched once into `~/.local/share/tinyaisense/bench`.
- TTS: `say` of 10 English and 10 Russian sentences, to the first 10 ms frame at the device. p95 ≤150 ms.
- Interrupt, with `--load`: a press to `button.down` at the SDK, while other simulated devices speak and listen. p99 ≤20 ms. A miss here calls for moving STT and TTS to a worker subprocess (ADR 0004).

A gate fails only if 2 of its 3 runs miss. The bench skips, writing nothing, if the load average is over 2 at the start (`--max-loadavg`). Each run writes a report, in Markdown and JSON, into `./acceptance/` (`--out`), with the numbers, the hub's version and the machine's CPU, governor and load.

On every push to main, the model tier (`.github/workflows/model.yml`) runs `bench speech --load` on the hub PC, a self-hosted runner that `.github/runner/setup.sh` registers, and pushes its report to the `acceptance` branch.

## Firmware

The firmware in `firmware/` is an ESP-IDF 5.5 project in plain C11, built with PlatformIO; `idf.py build` works too. Each Board gets its own binary, picked by a Kconfig choice. The pure-C audio code in `firmware/components/tas_audio` (DSP, ramps, the speaker jitter buffer and the DMA-patch planner) makes no IDF calls, so it's tested on the PC under ASan and UBSan:

```sh
cd firmware
pio test -e native            # the pure-C libraries, with Unity
pio run -e m5stack-atom-echo  # the Atom Echo binary; add `-t upload` to flash it
pio check -e m5stack-atom-echo -e native --fail-on-defect=low --fail-on-defect=medium --fail-on-defect=high
```
