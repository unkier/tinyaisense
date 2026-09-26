# Hub-side STT, TTS and VAD engine options

Research for [#5](https://github.com/unkier/tinyaisense/issues/5). Checked 2026-09-26 against primary sources: official repos, model cards, vendor docs and pricing pages, and author-published benchmarks.

**The question.** Which STT, TTS and VAD engines fit a Python **hub** (PC or Raspberry Pi 5 on the LAN) that handles 16 kHz mono speech **streams** from **devices**?

**Budget.** The latency targets are mouth→hub ≤150 ms and hub→speaker start ≤200 ms.

**Language scope.** English, Russian, and general multilingual breadth.

**How to read the numbers:**
- **Vendor claims.** Latency and WER figures come from each vendor or author unless noted otherwise, so treat them as vendor claims.
- **WER figures aren't comparable across rows.** They come from different datasets, normalization methods and leaderboard versions.
- **Open ASR Leaderboard (OASR).** OASR was re-baselined on 2026-09-25 ([results csv](https://huggingface.co/datasets/hf-audio/open-asr-leaderboard-results/blob/main/english_short_latest.csv)), so "OASR now" differs from older model-card figures. OASR has no Russian track.
- **"unverified"** means no primary source was found.

## TL;DR

- **VAD.** Use **Silero VAD v6** (MIT, 32 ms frames at 16 kHz, language-agnostic, negligible CPU) on the hub. Optionally add **Pipecat Smart Turn v3.2** for end-of-turn detection (BSD-2, ~12 ms on CPU, covers EN and RU). webrtcvad is legacy.
- **Local runtime.** **sherpa-onnx** (Apache-2.0, aarch64 wheels, very active) can serve as one runtime for local STT, TTS and VAD. It runs Silero and TEN VAD, streaming Zipformer / Nemotron / T-one, offline Parakeet / GigaAM, and Piper / Kokoro voices.
- **Local STT, EN and RU:**
  - **Best quality:** **Parakeet TDT 0.6b v3**, fed VAD-segmented utterances (CC-BY-4.0, 25 EU languages incl. RU, FLEURS RU 5.5%).
  - **Streaming partials in EN and RU:** **Nemotron 3.5 ASR Streaming**. It's new (June 2026) and its CPU speed is unverified.
  - **On an RPi 5:**
    - English: **Moonshine v2 streaming** (MIT; 0.24–0.8 s end-of-speech→final on Pi 5).
    - Russian: the small streaming Zipformer RU, or GigaAM v3 run offline.
  - **Widest languages (99):** Whisper large-v3 / turbo. It has no native streaming and needs a GPU.
- **Local TTS.** **Piper** is the only well-maintained, Pi-viable engine that covers EN, RU and 40+ other languages.
  - It streams per sentence. Its code is now GPL-3.0; the sherpa-onnx runtime avoids that.
  - Its RU voices carry licence caveats.
  - **Kokoro has no Russian** and is slower than real time on a Pi 4.
- **Cloud STT with RU:**
  - **Deepgram Flux multi**: built-in end-of-turn detection (~260 ms), $0.0078/min.
  - **ElevenLabs Scribe v2 Realtime**: ~150 ms, 90+ languages, $0.39/hr.
  - Google, Azure and OpenAI all stream too, at ~$1/hr.
- **Cloud TTS with RU:**
  - **ElevenLabs Flash v2.5**: ~75 ms model latency, 32 languages, native `pcm_16000`.
  - **Azure Neural**: $15 per 1M chars, cheapest with RU.
  - **Deepgram Aura-2 has no RU.**
- **200 ms TTS budget.** Meeting it requires the hub to chunk text at clause boundaries and, for cloud, to keep a pre-opened WebSocket or gRPC stream. Local Piper on x86 fits the budget. On a Pi, it needs measuring.

## 1. STT (speech-to-text)

### 1.1 Local STT: streaming, latency, quality, languages

| Engine / model | Streaming | Latency (source) | EN WER | RU WER | Languages |
|---|---|---|---|---|---|
| **faster-whisper** (large-v3, large-v3-turbo, distil) | **No native streaming.** Offline, with a VAD filter. Streaming only through wrappers: [WhisperLive](https://github.com/collabora/WhisperLive), [SimulStreaming](https://github.com/ufal/SimulStreaming) (GPU with ≥10 GB), and [whisper_streaming](https://github.com/ufal/whisper_streaming) (now outdated, ~3.3 s latency) | End-of-speech→final on Linux x86: large-v3 16.9 s, small 3.4 s, tiny 1.1 s ([Moonshine benchmark](https://github.com/moonshine-ai/moonshine/blob/main/docs/moonshine-vs-whisper.md), run by a competitor) | OASR now: large-v3 5.78, turbo 6.36, distil-large-v3.5 5.40 | large-v3: FLEURS 3.1, CommonVoice 9.1 ([GigaAM eval](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md)) | large-v3 and turbo: ~99, RU yes. **distil: EN only** ([card](https://huggingface.co/distil-whisper/distil-large-v3.5)) |
| **whisper.cpp** | `whisper-stream` example, which re-decodes a sliding window every 500 ms. The README calls it "naive" ([stream](https://github.com/ggml-org/whisper.cpp/tree/master/examples/stream)) | Same Whisper cost. Pi 5: base.en is OK, small.en saturates the CPU ([discussion](https://github.com/ggml-org/whisper.cpp/discussions/166), anecdotal) | same as Whisper | same as Whisper | ~99 |
| **Vosk** (Kaldi) | **Native partials** (`PartialResult`) | No official RTF | en-us-0.22: LibriSpeech 5.69; small-en 9.85 ([models](https://alphacephei.com/vosk/models)) | ru-0.42 (1.8 GB): golos 4.4. small-ru-0.22: golos 11.79 | 20+, RU yes |
| ↳ Vosk new-gen streaming Zipformer RU | Native streaming via [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 24 MB int8 | – | CommonVoice 11.3 ([card](https://huggingface.co/alphacep/vosk-model-small-streaming-ru)) | RU |
| **Moonshine v2** (streaming) | **Native streaming** with cached encoder and decoder | End-of-speech→final: Tiny 69 ms, Small 165 ms, Medium 269 ms on x86. On **RPi 5: 237, 527 and 802 ms** ([table](https://github.com/moonshine-ai/moonshine/blob/main/docs/moonshine-vs-whisper.md); the Pi figures [predate a build fix, so they read pessimistically](https://github.com/moonshine-ai/moonshine/blob/main/docs/using/benchmarks.md)) | OASR (old version): Medium 6.65, Small 7.84, Tiny 12.0 | **no RU model** | Streaming: en, de, es, ja, zh, ar, tl, vi ([models](https://github.com/moonshine-ai/moonshine/blob/main/docs/models/available-models.md)) |
| **Parakeet TDT 0.6b v2** | Offline. NeMo supports buffered chunking | RTFx 6025 on GPU (OASR) | OASR now 4.70 | – | **EN only** |
| **Parakeet TDT 0.6b v3** | Offline (chunked script) | sherpa-onnx int8, 2 threads: RTF 0.33 ([docs](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html)). onnx-asr on a Ryzen 9800X3D: RTFx 36 | OASR now 4.86 | **FLEURS 5.51** ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)) | **25 EU languages, RU yes** |
| **Nemotron 3.5 ASR Streaming 0.6b** (June 2026) | **Native streaming**, chunks of 80–1120 ms, with punctuation and capitalization | Card lists GPU only. sherpa-onnx int8 build exists; CPU and Pi speed unverified | FLEURS 7.91 at 1.12 s, 9.43 at 80 ms | **FLEURS 9.17 at 1.12 s, 10.84 at 80 ms** ([card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)) | 40 locales. 19 "transcription-ready", incl. EN and RU |
| Nemotron Speech Streaming en 0.6b | Native streaming, 80–1120 ms | sherpa-onnx int8 at 560 ms, 1 thread: RTF 0.16 ([docs](https://k2-fsa.github.io/sherpa/onnx/nemo/nemotron-streaming.html)) | OASR now 5.25 | – | EN |
| parakeet_realtime_eou_120m-v1 | Streaming, plus an end-of-utterance token | EOU p50 160 ms, p90 280 ms | avg 9.30 | – | EN |
| **GigaAM v3** (Sber) | Offline, ≤25 s per call, so it needs VAD segmentation | CPU-friendly; ONNX via onnx-asr and sherpa-onnx | FLEURS 9.4 (multilingual 600M) | **avg 8.3 over 10 RU sets** ([eval](https://github.com/salute-developers/GigaAM/blob/main/evaluation.md)) | RU (plus kk, ky, uz in the multilingual line) |
| **T-one** (T-Bank) | **Native streaming**, 300 ms chunks | 4 cores and 8 GB recommended ([repo](https://github.com/voicekit-team/T-one)) | – | call-center 8.63, CV19 5.32 | **RU only**. Telephony-tuned, 8 kHz input (inferred from the pipeline code) |
| Kyutai STT 1b / 2.6b | Native streaming (0.5 s / 2.5 s delay), with semantic VAD | GPU (L40S) or MLX ([repo](https://github.com/kyutai-labs/delayed-streams-modeling)) | OASR 2.6b: 5.57 | **no** | en+fr / en |
| Voxtral Mini 4B Realtime | Native streaming, 80–2400 ms delay | GPU with ≥16 GB | FLEURS 4.90 | FLEURS 6.02 | 13, RU yes |
| Canary-1b-v2 / Qwen3-ASR | Offline / streaming through vLLM only | GPU | OASR 5.71 / 4.31 (Qwen3-ASR-1.7B is the best open model on the board) | RU supported; WER not published | 25 / 30+ |

### 1.2 Local STT: hardware, licence, Python maturity

| Engine | RPi 5 | x86 CPU / GPU | Code licence | Weights licence | Python package (latest) |
|---|---|---|---|---|---|
| faster-whisper | Installs (ctranslate2 ships aarch64 wheels). Finals take 5.9 s (tiny) and 10.4 s (small), so **too slow** | CPU for small models; GPU for large-v3 in real time | MIT | Whisper MIT; large-v3 Apache-2.0 on HF | `faster-whisper` 1.2.1 (2025-10). Slow maintenance |
| WhisperLive | – | CPU, GPU, OpenVINO | MIT | Whisper | `whisper-live` 0.10.0 (2026-09), active |
| whisper.cpp | Officially supported; only tiny/base are practical | CPU, CUDA, Vulkan | MIT | Whisper | `pywhispercpp` 1.5.1 (2026-08). No streaming in the bindings |
| Vosk | **Yes**: small models are ~50 MB and ~300 MB RAM | CPU | Apache-2.0 | Apache-2.0 (mostly) | `vosk` **0.3.45 (2022-12)**. Stale; the new-gen models run through sherpa-onnx |
| Moonshine | **Yes**, with official Pi 5 numbers | CPU-first (onnxruntime) | MIT | **MIT** for all streaming and English models. Legacy non-streaming ar/ja/ko/zh/es/uk/vi use the [Moonshine Community License](https://github.com/moonshine-ai/moonshine/blob/main/LICENSE) | `moonshine-voice` 0.1.5 (2026-08), aarch64 wheels, active |
| Parakeet v2 / v3 | No Pi 5 numbers. Cortex-A53 (much weaker): RTFx 1.0 | CPU (ONNX) or GPU (NeMo) | Apache-2.0 | **CC-BY-4.0** | `nemo-toolkit` 3.0.0 (heavy), or `onnx-asr` 0.12.0 (light, offline) |
| Nemotron 3.5 / en streaming | Unverified | GPU per the card; int8 CPU via sherpa-onnx | – | 3.5: **OpenMDW-1.1**. en: NVIDIA Open Model License | via `sherpa-onnx` |
| sherpa-onnx (runtime) | **Yes** (aarch64 is first-class) | CPU and GPU | Apache-2.0 | per model | `sherpa-onnx` 1.13.8 (2026-09-10), very active |
| GigaAM / T-one | Unverified; plausible at 71–240M params | CPU | MIT / Apache-2.0 | MIT / Apache-2.0 | GigaAM: install from the repo (PyPI package is stale). T-one: not on PyPI |

### 1.3 Cloud STT

Latency figures are vendor claims, usually server-side with network time excluded. Prices are pay-as-you-go USD as of 2026-09.

| Vendor / model | Protocol, partials | End-of-speech / latency | EN / RU / languages | Streaming price | Free tier | Python SDK |
|---|---|---|---|---|---|---|
| **Deepgram Flux** (`flux-general-en`, `flux-general-multi`) | WS `/v2/listen`. Built-in **EndOfTurn / EagerEndOfTurn** events. linear16 at 16 kHz OK ([quickstart](https://developers.deepgram.com/docs/flux/quickstart)) | ~260 ms end-of-turn | multi: 10 languages incl. **RU** ([models](https://developers.deepgram.com/docs/models-languages-overview)) | EN $0.0065/min (promo), multi $0.0078/min ([pricing](https://deepgram.com/pricing)) | $200 credit | `deepgram-sdk` 7.10 (async) |
| Deepgram Nova-3 | WS `/v1/listen` with interim results | – | 45+ languages, RU yes | $0.0048–0.0058/min (promo) | $200 credit | same |
| **OpenAI gpt-live-transcribe** (2026-07) | Realtime transcription session over WS or WebRTC, with deltas. **No server VAD**, so the hub commits turns itself. 24 kHz PCM input, so resampling is needed ([guide](https://developers.openai.com/api/docs/guides/realtime-transcription), [model](https://developers.openai.com/api/docs/models/gpt-live-transcribe)) | Tunable; no ms figure published | Multilingual with language hints, RU yes | $0.017/min ([pricing](https://developers.openai.com/api/docs/pricing)) | none | `openai` 3.19 (async realtime) |
| OpenAI gpt-transcribe / gpt-4o-(mini-)transcribe | Transcribes a committed turn; server_vad / semantic_vad available on VAD-capable models ([VAD](https://developers.openai.com/api/docs/guides/realtime-vad)) | After the commit | ~57 languages, RU yes | $0.0045 / $0.006 / $0.003 per min | none | same |
| **ElevenLabs Scribe v2 Realtime** | WS with partial and committed transcripts. Commits are manual or VAD-based. Takes `pcm_16000` ([docs](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies)) | ~150 ms ([models](https://elevenlabs.io/docs/overview/models)). The docs also mention a "first 2 s" warm-up, so test it | 90+ languages. RU in the "≤5% WER" tier ([STT](https://elevenlabs.io/docs/capabilities/speech-to-text)) | $0.39/hr ([API pricing](https://elevenlabs.io/pricing/api)) | 10k credits/mo, non-commercial | `elevenlabs` 2.69 (async) |
| Google STT v2 Chirp 3 | gRPC bidirectional, streams ≤5 min. Interim results, voice-activity events, `endpointing_sensitivity` ([Chirp 3](https://docs.cloud.google.com/speech-to-text/v2/docs/chirp_3-model)) | – | 100+ languages, **ru-RU GA** | $0.016/min ([pricing](https://cloud.google.com/speech-to-text/pricing)) | – | `google-cloud-speech` 2.40 |
| Azure Speech real-time | Speech SDK over WS, with `recognizing` partials | – | ~146 locales, ru-RU yes ([languages](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support)) | $1.00/hr ([pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/)) | 5 hr/mo | `azure-cognitiveservices-speech` 1.51 (aarch64 wheel; callback-based) |
| AssemblyAI Universal-Streaming | WS | – | **No RU** (6 languages; RU only via whisper-rt) ([docs](https://www.assemblyai.com/docs/streaming/universal-streaming/multilingual-transcription)) | $0.15/hr, billed on session time | $50 | `assemblyai` |
| Yandex SpeechKit v3 | gRPC | – | RU-first | ~$0.32/hr ([pricing](https://aistudio.yandex.ru/docs/en/speechkit/pricing)) | – | `yandex-speechkit` |

**Privacy:**
- **OpenAI:** doesn't train on API data. Its audio endpoints are eligible for Zero Data Retention ([your-data](https://developers.openai.com/api/docs/guides/your-data)).
- **ElevenLabs:** retains data unless you're on Enterprise, whose Zero Retention Mode turns that off ([ZRM](https://elevenlabs.io/docs/eleven-api/resources/zero-retention-mode)).
- **Deepgram and Azure:** retention terms weren't checked.

## 2. TTS (text-to-speech)

### 2.1 Local TTS

| Engine | Streaming | Speed (source) | Output rate | Quality | EN / RU / langs | Licence: code / weights | Python package (latest) |
|---|---|---|---|---|---|---|---|
| **Piper** ([OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)) | **Per sentence**: `synthesize()` yields one chunk per sentence ([API](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md)) | RPi 4, medium voice, 4 threads: RTF 0.36 ([sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/vits.html)). i5: ~17× real time ([HA blog](https://www.home-assistant.io/blog/2023/04/27/year-of-the-voice-chapter-2/)). **No primary Pi 5 number** | low/x_low 16 kHz; medium/high 22.05 kHz | Not ranked on TTS Arena | EN yes. **RU yes** (4 medium voices). 43 languages, 177 voices ([VOICES](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md)) | **GPL-3.0** (the old rhasspy/piper was MIT; archived 2025-10). Voices have **per-voice dataset licences** (below) | `piper-tts` 1.8.0 (2026-09-04), aarch64 wheels. README asks for maintainers |
| **Kokoro-82M** ([card](https://huggingface.co/hexgrad/Kokoro-82M)) | Per segment (`KPipeline` generator). `kokoro-onnx` has async `create_stream()` | **RPi 4: RTF 3.19, so slower than real time** ([sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html)). x86: no primary number | 24 kHz | TTS Arena V2: #30 of 40, ELO 1482 | EN yes. **RU no**. 9 language codes ([VOICES](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)) | Apache-2.0 / Apache-2.0 | `kokoro` 0.9.4 (2025-04, stalled). `kokoro-onnx` 0.6.1 (2026-08, active) |
| **sherpa-onnx TTS** (runtime) | Sentence-level `callback` | Publishes RTF tables per model | per model | – | Runs Piper voices (incl. the 4 RU), Kokoro, Kitten, Matcha, MMS, Pocket TTS, Supertonic | Apache-2.0 runtime; voice licences still apply | `sherpa-onnx` 1.13.8 |
| Silero TTS ([repo](https://github.com/snakers4/silero-models)) | Per sentence | V5 on an i9: 36× real time on 1 thread ([wiki](https://github.com/snakers4/silero-models/wiki)) | 8, 24 or 48 kHz | RU UTMOS 3.04 (ground truth 3.08) | **Best RU** (auto-stress). EN old v3. Many CIS languages | **Main models CC BY-NC-SA** (non-commercial). `v5_cis_base` RU is MIT, but needs manual stress marks | `silero` 0.5.5 (2026-02), needs torch |
| Kyutai Pocket TTS ([repo](https://github.com/kyutai-labs/pocket-tts)) | **Audio streaming** | ~200 ms to first chunk, ~6× real time on 2 M4 cores | ~24 kHz (unverified) | – | en, fr, de, pt, it, es. **No RU** | MIT / CC-BY-4.0 | `pocket-tts` 3.3.0 (2026-09-24) |
| Supertonic 3 | Unverified | Claims RTF 0.3 on an e-reader | 44.1 kHz | RU WER 3.99 (self-reported) | 31 languages, RU yes | MIT / OpenRAIL-M | **Upstream archived** |
| Chatterbox Multilingual | Not natively streaming | GPU | 24 kHz | Arena #28 | 23 languages, RU yes | MIT, with a watermark on every output | `chatterbox-tts` 0.1.7 |
| XTTS-v2 | Streaming, <200 ms on GPU | GPU | 24 kHz | – | 17 languages, RU yes | **CPML (non-commercial)**. Coqui is defunct; the idiap fork is maintained | `coqui-tts` 0.27.5 |

**Skipped extras:** NeuTTS, Kitten, VibeVoice-Realtime, Orpheus, F5, Dia and CSM. Each fails on at least one of three grounds: no RU, GPU-only, or a non-commercial licence.

**Piper voice licences** ([MODEL_CARD per voice](https://huggingface.co/rhasspy/piper-voices)):
- **Russian voices:**
  - `ru_RU-denis` and `ru_RU-dmitri` are CC0.
  - `ru_RU-irina` is unknown (RHVoice).
  - `ru_RU-ruslan` is CC BY-NC-SA.
- **The lessac lineage caveat:**
  - All four RU voices are fine-tuned from `en_US-lessac`.
  - lessac's source dataset is **research-only** ([licence](https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html)).
  - Whether that carries over to the fine-tuned weights is an open legal question.
- **Clean EN options:** `en_US-ljspeech-high` (public domain), and `en_US-libritts_r-medium` / `en_GB-alba` (CC BY 4.0).

**Time to first audio (derived, not measured).** TTFA ≈ first-chunk audio length × RTF. For a 1.5 s first sentence:
- **Piper, i5-class x86:** ~0.09 s, which meets the 200 ms target.
- **Piper medium, RPi 4:** ~0.54 s, which misses it.
- **Hub requirement:** to hit 200 ms on a Pi, the hub must split text at clause or punctuation boundaries and synthesise the first short chunk immediately.
- **Resampling:** 22.05 k or 24 k output needs resampling to 16 k. Only Piper's low and x_low voices are native 16 k, and those are EN only.

### 2.2 Cloud TTS

| Vendor / model | Streaming | TTFB (vendor claim) | EN / RU / languages | Price per 1M chars | Free tier |
|---|---|---|---|---|---|
| **ElevenLabs Flash v2.5** | WS text-in streaming (`stream-input`). Outputs **`pcm_16000`** and `ulaw_8000` ([API](https://elevenlabs.io/docs/api-reference/text-to-speech/stream)) | ~75 ms model time, with network on top ([latency guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization)) | 32 languages, RU yes | $50. Multilingual v2 / v3 cost $100 and cover 29 / 70+ languages ([API pricing](https://elevenlabs.io/pricing/api)) | 10k credits/mo |
| Deepgram Aura-2 | WS `/v1/speak` with text streaming; linear16 output ([latency doc](https://developers.deepgram.com/docs/text-to-speech-latency)) | ~90 ms, p95 <200 ms ([blog](https://deepgram.com/learn/engineering-real-time-low-latency-voice-ai-at-scale)) | 7 languages. **No RU** ([models](https://developers.deepgram.com/docs/tts-models)) | $30 | $200 credit |
| OpenAI gpt-4o-mini-tts / tts-1 | HTTP chunked streaming; `pcm` output at 24 kHz ([guide](https://developers.openai.com/api/docs/guides/text-to-speech)) | Not published | 57 languages incl. RU; voices are English-optimised | tts-1 $15. mini-tts is token-billed ([model](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts)) | none |
| Google Chirp 3 HD / Gemini-TTS | gRPC `StreamingSynthesize` (Preview); PCM output ([Chirp 3 HD](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd)) | Not published | 53 locales incl. ru-RU | $30; first 1M chars/mo free ([pricing](https://cloud.google.com/text-to-speech/pricing)) | 1M chars/mo |
| **Azure Neural / HD** | SDK over WS; text-stream input via the v2 endpoint (Python). Pre-connect advised ([latency doc](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-lower-speech-synthesis-latency)) | Not published | 100+ locales. RU voices: Svetlana, Dmitry, Dariya, plus HD voices | Neural **$15**, HD $22 | 0.5M chars/mo |
| Cartesia Sonic-3.6 | WS / SSE | <90 ms ([docs](https://docs.cartesia.ai/build-with-cartesia/tts-models/latest)) | 44 languages, RU yes | ~$37–50 ([pricing](https://cartesia.ai/pricing)) | 20k credits/mo |

**Integration notes:**
- **Deepgram needs a second vendor for Russian.** Deepgram STT covers RU, but its TTS doesn't.
- **ElevenLabs, Google, Azure and OpenAI cover both STT and TTS in EN and RU.** Cartesia covers both languages for TTS only.

## 3. VAD and turn detection

### 3.1 Frame-level VAD

| VAD | Frame / latency | Accuracy (author-published) | CPU / RPi | Licence | Python |
|---|---|---|---|---|---|
| **Silero VAD v6** ([repo](https://github.com/snakers4/silero-vad)) | **512 samples at 16 kHz (32 ms)**; 8 k and 16 k only | Multi-domain ROC-AUC: **v6 0.97**, v5 0.96, TEN 0.93, WebRTC 0.73 ([wiki](https://github.com/snakers4/silero-vad/wiki/Quality-Metrics)) | ~0.2 ms per chunk on 1 x86 thread ([perf](https://github.com/snakers4/silero-vad/wiki/Performance-Metrics)). ~2 MB model. No Pi number, but negligible cost is expected | MIT. Language-agnostic (trained on 6000+ languages) | `silero-vad` 6.2.3 (2026-09-23). The pip package pulls in torch; on a Pi, use the raw ONNX model with onnxruntime, or sherpa-onnx |
| webrtcvad | 10/20/30 ms frames; 8, 16, 32 and 48 kHz; aggressiveness 0–3 | Worst in both the Silero and TEN comparisons (ROC-AUC 0.73) | Negligible (GMM) | MIT | `webrtcvad` 2.0.10 (**2017**, unmaintained). Use `webrtcvad-wheels` 2.0.14 (2024, aarch64 wheels) |
| TEN VAD ([repo](https://github.com/TEN-framework/ten-vad)) | 10 or 16 ms hop, 16 kHz only | Claims to beat Silero and to end speech faster. **This contradicts Silero's wiki.** Both are vendor claims | Very cheap. Prebuilt Linux library is x64 only; on arm64, use ONNX or sherpa-onnx | **Apache-2.0 plus a non-compete clause** against Agora | Install from git; PyPI provenance unclear |
| Picovoice Cobra | Frame-based | Own benchmark | RPi Zero RTF 0.037 | **Proprietary**, needs an AccessKey | `pvcobra` 3.0.3 |
| pyannote segmentation-3.0 | 10 s windows, offline | – | Heavy | MIT (gated) | Not for real time |

### 3.2 Turn / end-of-utterance detection

Turn detection is optional and sits on top of VAD. It decides whether the person has finished speaking or only paused.

| Model | Input | Latency | Languages | Licence | Verdict |
|---|---|---|---|---|---|
| **Pipecat Smart Turn v3.2** ([repo](https://github.com/pipecat-ai/smart-turn), [card](https://huggingface.co/pipecat-ai/smart-turn-v3)) | Audio: 16 kHz, ≤8 s, run when VAD sees silence | ~12 ms on a modern CPU; 60 ms on an AWS Graviton c8g.medium ([blog](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/)) | 23, **incl. EN and RU** | **BSD-2-Clause** | Best fit: audio-native, 8 MB int8 ONNX, cheap on CPU |
| LiveKit turn-detector | Text (transcript), Qwen2.5-0.5B | 50–160 ms | 14 incl. RU | **LiveKit Model License**: usable only inside LiveKit Agents | Ruled out by its licence |
| Kyutai semantic VAD | Built into Kyutai STT | GPU | en/fr | CC-BY-4.0 | Only useful if you adopt Kyutai STT |
| Deepgram Flux / OpenAI semantic VAD | Cloud (see §1.3) | – | – | Commercial | Comes with the cloud STT |

## 4. Shortlist

| Role | PC (x86 CPU) | PC + NVIDIA GPU | Raspberry Pi 5 | Cloud |
|---|---|---|---|---|
| **VAD** | Silero VAD v6 (ONNX) | same | same (onnxruntime or sherpa-onnx, no torch) | vendor VAD / end-of-turn events (Deepgram Flux, ElevenLabs) |
| **End of turn** (optional) | Smart Turn v3.2 | same | same (~60 ms on a Graviton-class core) | Deepgram Flux EndOfTurn |
| **STT: EN+RU final** | Parakeet TDT 0.6b v3 (onnx-asr / sherpa-onnx), after VAD | Parakeet v3 (NeMo), or Whisper large-v3-turbo for 99 languages | RU: GigaAM v3 or the small streaming Zipformer RU. EN: Moonshine v2 | Deepgram Flux multi, or ElevenLabs Scribe v2 RT |
| **STT: streaming partials** | Nemotron 3.5 streaming (sherpa-onnx int8; **measure it**) | Nemotron 3.5 streaming (NeMo) | Moonshine v2 (EN only); small streaming Zipformer RU | same as above |
| **TTS** | Piper (EN, RU, 40+ languages). Kokoro for nicer EN only | Piper or Kokoro (Chatterbox and XTTS are licence- or streaming-limited) | Piper low/medium voices (**measure TTFA**) | ElevenLabs Flash v2.5 (all-round); Azure Neural (cheapest with RU); Deepgram Aura-2 (EN only) |

**Implications for hub design:**
1. **Hide engines behind a small hub-side interface.** STT (push PCM, get partial and final text), TTS (text in, 16 kHz PCM chunks out) and VAD should each sit behind one. There is no single winner across Pi, PC, GPU and cloud, or across EN and RU.
2. **Run VAD on the hub regardless of engine choice.** Offline-final engines (Parakeet, GigaAM, Whisper) need utterance segmentation. OpenAI's live transcription needs the hub to commit turns.
3. **The TTS path needs clause-level text chunking, resampling and warm connections.**
   - Chunk text at clause level so the first chunk is short.
   - Resample 22.05, 24 and 44.1 kHz output to 16 kHz.
   - Keep cloud WebSocket or gRPC connections warm to hit ≤200 ms.
4. **Licences to watch:**
   - Piper code is GPL-3.0.
   - Piper RU voices descend from lessac, whose dataset is research-only, and ruslan is NC.
   - Silero TTS is NC.
   - XTTS is CPML (non-commercial).
   - TEN VAD has a non-compete clause.
   - The LiveKit turn-detector is framework-locked.
   - Parakeet and Canary are CC-BY-4.0 (attribution required).
   - Nemotron 3.5 is OpenMDW-1.1.
5. **Measure before committing** (not covered by any primary source):
   - Pi 5 RTF for Piper, Parakeet, Nemotron 3.5 and GigaAM.
   - Real cloud TTFB from the hub's network location.

## 5. Open / unverified

- **Moonshine:** its Pi 5 figures predate a build fix, so they're pessimistic.
- **Pi 5 numbers are missing** for Piper, Kokoro, Parakeet, Nemotron, GigaAM, T-one and Silero VAD. Only Pi 4 figures (via sherpa-onnx) exist.
- **VAD accuracy:** every comparison is vendor-published, and Silero and TEN contradict each other.
- **Missing figures:**
  - Canary-1b-v2 RU WER.
  - Speechmatics real-time price.
  - Google's pricing tier for chirp_3.
  - Per-minute equivalent for OpenAI TTS.
- **ElevenLabs Scribe v2 RT:** a "2 s" warm-up note in its docs needs testing.
- **Azure Speech SDK on Pi OS:** its Linux support list tops out at Debian 12. If Pi OS is Debian 13-based, test it.
