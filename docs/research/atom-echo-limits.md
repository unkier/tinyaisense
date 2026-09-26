# M5Stack Atom Echo: hardware limits (duplex, RAM, codec budget)

Research for [#3](https://github.com/unkier/tinyaisense/issues/3). Researched 2026-09-26.

Legend: **[M]** is a measured or vendor-documented fact from the cited source. **[I]** is my inference or estimate. **[A]** is an anecdotal report (one user, not a controlled measurement).

## TL;DR

| Question | Answer |
|---|---|
| Full duplex? | **No. It is a hardware limit, not a firmware one.** GPIO33 carries both the mic's PDM clock (must run at 1.0 to 3.25 MHz) and the amp's I2S LRCK (runs at the sample rate, 8 to 96 kHz). One pin can't carry both. M5Stack's own comparison table lists the Echo as "Half-duplex". The only workaround is fast time-division switching, which is still half duplex. |
| Heap after Wi-Fi (+TLS)? | About 275 KiB of byte-addressable heap at boot, plus 59 KiB of IRAM that only allows 32-bit access [M]. Expect **roughly 145 to 185 KB free after Wi-Fi STA** [M: Espressif's iperf ranks], and **roughly 105 to 165 KB after one TLS session** (TLS costs 22 to 42 KB [M]). |
| Opus enc+dec at 16 kHz mono? | **Fits, but it's tight** [I]. Decode takes about 10% of one core [M]. Encode at complexity 0 takes about 50 to 70% of one core [I, extrapolated]. RAM is about 55 to 100 KB of codec state plus a 24 to 40 KB task stack. xiaozhi-esp32 ships this setup on a classic ESP32 with no PSRAM. Because the Echo is half duplex, encode and decode never need to run at the same time on this Device. |
| ADPCM / raw PCM? | Raw PCM at 16 kHz/16-bit is 256 kbit/s: no CPU cost, no codec delay, trivial for LAN Wi-Fi. ESPHome already streams raw PCM from this exact Device [M]. IMA-ADPCM is 64 kbit/s at about 1 to 3% CPU [M, S3]. **Recommendation: raw PCM on the Echo.** |
| Rates / bit depths | Mic: PDM to PCM on I2S0 only, **16-bit only**. 16 kHz works (PDM clock 1.024 or 2.048 MHz). The mic's clock range allows fs from about 7.8 to 50 kHz. Speaker: NS4168 takes 8 to 96 kHz I2S. Use 16-bit, **sent as stereo** (mono output sounds tinny on the ESP32). |
| Quality issues | Mic: DC offset (noise floor about -30 dB, about -60 dB after DC removal [M]) and low level, which needs gain. Speaker: small (0.5 or 0.8 W, the M5 docs disagree) and quiet. M5 warns against DC, long white noise, and full-scale square waves. |
| Pins | Speaker BCLK G19, **LRCK G33**, DATA G22. Mic **CLK G33**, DATA G23. Button G39 (active low). LED SK6812 on G27. Grove G26/G32. |

## 1. Hardware and pin map

- SoC: ESP32-PICO-D4, 240 MHz dual core, 4 MB flash, **no PSRAM**. The product has been renamed "Atom Voice" but keeps SKU C008-C [M, [M5 docs](https://docs.m5stack.com/en/atom/atomecho)].
- 520 KB SRAM in total (320 KB DRAM, 200 KB IRAM). At most 160 KB of it can be static DRAM [M, [ESP-IDF memory types](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/memory-types.html)].
- Mic: Knowles SPM1423HM4H-B PDM. Amp: NS4168 I2S class-D. LED: SK6812. The speaker is "0.8 W" on the product page and "0.5W@8Ω" in M5's comparison table [M, [M5 docs](https://docs.m5stack.com/en/atom/atomecho), [Atomic Voice Base docs](https://docs.m5stack.com/en/atom/Atomic%20Echo%20Base)].

| Capability | Signal | GPIO | Source / notes |
|---|---|---|---|
| Speaker (Actuator) | NS4168 BCLK | G19 | [M] M5 pin map + schematic |
| Speaker | NS4168 LRCK/WS | **G33** | [M] shared net "SYS LRCK" in the schematic |
| Speaker | NS4168 SDATA | G22 | [M] |
| Mic (Sensor) | SPM1423 CLK | **G33** | [M] same net as the amp LRCK |
| Mic | SPM1423 DATA | G23 | [M] |
| Button (Sensor) | input | G39 | [M] ESPHome config uses `inverted: true` (active low). GPIO39 is input-only. |
| RGB LED (Actuator) | SK6812 data | G27 | [M] ESPHome: 1 LED, GRB order, RMT driver |
| IR TX | | G12 | [I] The Echo page lists "1 x IR-TX" but no pin. G12 is the IR pin on ATOM Lite/Matrix. |
| Grove | | G26, G32 | [M] |
| Bottom header | | G21, G25 free. G19/G22/G23/G33 **reserved** | [M] M5 warns that reusing these pins "may cause permanent hardware damage". |

Sources: [M5 Atom Echo docs](https://docs.m5stack.com/en/atom/atomecho), [schematic image](https://static-cdn.m5stack.com/resource/docs/products/atom/atomecho/atomecho_sch_01.webp), [ESPHome Atom Echo config @94dd3d1](https://github.com/esphome/wake-word-voice-assistants/blob/94dd3d17249bf391a1090cd3fd9cf189909605c3/m5stack-atom-echo/m5stack-atom-echo.yaml), [M5Unified pin tables](https://github.com/m5stack/M5Unified/blob/8b63555e7bd9ac71978c55ad868fd1d1d3b2ef3e/src/M5Unified.inl#L2802-L2807).

## 2. Duplex: why it's half duplex, and what workarounds exist

### Why [M]

1. **The pin is shared.** The schematic ties SPM1423 CLK and NS4168 LRCK to the same net on G33.
2. **The two clocks are incompatible.**
   - The SPM1423 needs a PDM clock of **1.0 to 3.25 MHz**. Below 1 kHz it sleeps. ([datasheet](https://m5stack.oss-cn-shenzhen.aliyuncs.com/resource/docs/datasheet/core/SPM1423HM4H-B_datasheet_en.pdf), p.3)
   - NS4168: "LRCLK frequency equals the sampling frequency", and the chip supports 8 to 96 kHz. ([datasheet, CN](https://m5stack.oss-cn-shenzhen.aliyuncs.com/resource/docs/datasheet/core/NS4168_CN_datasheet.pdf), §2, §9.2.1)
   - ESP-IDF drives the PDM RX clock at 64×fs or 128×fs, which is 1.024 or 2.048 MHz at 16 kHz. In standard mode, WS equals fs. ([ESP-IDF I2S](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/i2s.html))
3. **ESP32 I2S limits** (ESP-IDF I2S docs):
   - The PDM-to-PCM converter exists only on **I2S0**.
   - "PDM full-duplex is not supported". Standard-mode full duplex needs a shared BCLK/WS and identical frame timing.
   - On the ESP32, a simplex channel occupies the **whole** controller (both RX and TX).
4. **Vendor and ESPHome confirm it.**
   - M5's comparison table lists Atom Voice (Echo) as "Half-duplex" and the Atomic Voice Base as "Full-duplex".
   - M5Unified's example says: "Since the microphone and speaker cannot be used at the same time, turn off the speaker here" ([Microphone.ino](https://github.com/m5stack/M5Unified/blob/8b63555e7bd9ac71978c55ad868fd1d1d3b2ef3e/examples/Basic/Microphone/Microphone.ino#L50-L51)).
   - The ESPHome maintainer writes "the Echo doesn't support duplex audio" ([PR #99](https://github.com/esphome/wake-word-voice-assistants/pull/99)).
   - ESPHome puts the mic and speaker on one `i2s_audio` bus guarded by a mutex (`try_lock()`). Each switch tears the driver down and reinstalls it ([i2s_audio_microphone.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/i2s_audio/microphone/i2s_audio_microphone.cpp#L97-L109)).

### Workarounds evaluated

| Idea | Works? | Why |
|---|---|---|
| I2S0 = PDM RX, I2S1 = standard TX, both running | **No** | Both need to drive G33 with different clocks, and a GPIO has one output source [I]. One user tried every port/order permutation, then gave up: "If I call out.begin() last, I hear loud low frequency sound, else nothing" [A, [arduino-audio-tools #1742](https://github.com/pschatzmann/arduino-audio-tools/discussions/1742)]. |
| One port in full duplex | **No** | PDM full duplex is unsupported, and PDM RX can't pair with standard TX [M, ESP-IDF]. |
| Clock the mic from LRCK by raising fs | **No** | The mic needs ≥1 MHz but the NS4168 tops out at 96 kHz [M]. |
| Fast time-division switching (re-init the driver, or keep both ports and re-route G33 in the GPIO matrix) | **Yes, but still half duplex** | Same user: "It works if I set the pins before each change of direction" [A]. Switch cost: the mic's wake-up from sleep is ≤10 ms [M, datasheet], plus decimation settling and DMA prefill. Not measured end to end. |
| Rewire the mic CLK to the free G21/G25 | Maybe | Needs the case opened and a trace cut. Nobody has verified it [I]. Not a product path. |
| Different hardware | **Yes** | See §7. |

**Implications for tinyaisense [I]:**
- The Echo Device should advertise its audio as half duplex. The mic Stream and speaker Stream are mutually exclusive.
- Barge-in (interrupting playback by speaking) is impossible. Use the button Event instead.
- On the upside, there's no speaker-to-mic echo, so the Echo doesn't need AEC.

## 3. RAM budget (no PSRAM)

| Item | Size | Kind |
|---|---|---|
| Heap at boot, classic ESP32 (xiaozhi v2.2.4 build) | 6+144+14+111 = **275 KiB byte-addressable** + **59 KiB IRAM** | [M] `heap_init` log in [xiaozhi #1843](https://github.com/78/xiaozhi-esp32/issues/1843) |
| Spare DRAM with a "hello_world" app | ~290 kB | [M] [ESP-ADF design considerations](https://github.com/espressif/esp-adf/blob/master/docs/en/design-guide/design-considerations.rst) |
| Wi-Fi | "50kB+" to reserve | [M] same doc |
| Free heap during Wi-Fi iperf, ESP32 without PSRAM | 144.5 KB (default) / 170.2 (memory-saving) / 185.2 (minimum) | [M] [ESP-IDF Wi-Fi perf ranks](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/wifi-driver/wifi-performance-and-power-save.html) |
| TLS (mbedTLS, https_request example) | 42 KB default; **22 KB** with dynamic buffers | [M] [ESP-IDF mbedTLS](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/protocols/mbedtls.html) |
| xiaozhi (Opus enc+dec, Wi-Fi AP, display) on a no-PSRAM ESP32 | free internal 152–157 KB, minimum 133 KB | [M] same issue. Note: `MALLOC_CAP_INTERNAL` includes the IRAM region. |
| Bluetooth + Wi-Fi + audio | Not viable without PSRAM | [M] ESP-ADF doc |

Gotchas:
- On the classic ESP32, `MALLOC_CAP_INTERNAL` can return IRAM that only allows 32-bit access. Byte access to it causes a LoadStoreError, which crashed ESPHome on this exact Device ([esphome#18271](https://github.com/esphome/esphome/issues/18271)). Allocate audio buffers with `MALLOC_CAP_8BIT` or `MALLOC_CAP_DMA` [M].
- Watch the largest free block as well as the total free heap, because fragmentation matters [M, ESP-IDF RAM guide].

**Estimated budget for tinyaisense firmware [I]:**
1. About 150–180 KB free after Wi-Fi STA with lean Wi-Fi settings.
2. Minus 22–42 KB if TLS is used. LAN-only designs could skip TLS.
3. That leaves **about 110–160 KB** for app plus audio.

Raw-PCM audio needs little of this:
- 16 kHz/16-bit is 32 KB/s.
- A 200 ms jitter buffer is 6.4 KB.
- ESPHome uses a 512 ms (16 KB) mic ring buffer and sends 32 ms chunks ([voice_assistant.cpp](https://github.com/esphome/esphome/blob/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/voice_assistant/voice_assistant.cpp#L21-L25)).
- Opus would take about 55–100 KB of state plus a 24–40 KB stack, which is the largest single consumer.

## 4. Codec budget (CPU and RAM)

| Codec | CPU on the ESP32 @240 MHz | RAM | Kind |
|---|---|---|---|
| Raw PCM 16k/16-bit mono | ~0 | Buffers only | [M] ESPHome streams raw 16 kHz PCM from the Echo |
| IMA-ADPCM (4:1, 64 kbit/s) | ~2.4–2.7% (S3, 48 kHz stereo) | <1 KB | [M, S3] [esp_audio_codec](https://github.com/espressif/esp-adf-libs/blob/master/esp_audio_codec/README.md) |
| Opus **decode**, SILK 16 kHz mono 10 kbit/s | **RTF 0.095 (~10% of one core)**, 1.9 ms per 20 ms frame | state ~30–50 KB | [M] [micro-opus plain-ESP32 bench](https://github.com/esphome-libs/micro-opus/blob/main/examples/decode_benchmark/README.md) (fixed point) |
| Opus **encode**, SILK 16 kHz mono, complexity 0 | ESP32-S3: RTF 0.20–0.26 [M]. **Plain ESP32: ~0.5–0.7** [I] | state ~30–50 KB, 24–40 KB stack | [M] [micro-opus S3 encode bench](https://github.com/esphome-libs/micro-opus/blob/main/examples/encode_benchmark/README.md). [I] scaled by the ESP32/S3 SILK decode ratio (~2.6×). Consistent with "16kHz … complexity 1 will use 70% CPU" [A, [esp-libopus 2020](https://github.com/XasWorks/esp-libopus)] |
| Opus (Espressif lib), S3, 48 kHz stereo | enc 24.9% / dec 5.9% | enc 29.4 KB / dec 26.6 KB heap. Stack ~40 KB (enc) / ~20 KB (dec) | [M, S3] esp_audio_codec README |

- **Concurrent Opus encode and decode** would take about 60–80% of one core [I]. That's feasible when pinned to the app core with Wi-Fi on core 0, but leaves little headroom.
- Existence proof: xiaozhi-esp32 runs esp_audio_codec Opus on a no-PSRAM ESP32 (the `atommatrix-echo-base` board, same ESP32-PICO-D4). It encodes at complexity 0, 16 kHz mono, 60 ms frames with DTX/VBR, and decodes at 24 kHz, using a 24 KB codec task stack ([audio_service.h](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/audio/audio_service.h#L67-L79), [audio_service.cc](https://github.com/78/xiaozhi-esp32/blob/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/audio/audio_service.cc#L163-L170)) [M].
- xiaozhi does **not** support the plain Atom Echo (there is no board directory for it) [M].
- Build Opus in **fixed point**. Floating-point SILK encode fails real time even on the S3 [M, micro-opus].
- Opus adds algorithmic delay. For SILK it is the frame length (10–60 ms) plus 5 ms look-ahead plus up to 1.5 ms of resampling, so about 26.5 ms at 20 ms frames [M, [RFC 6716 §2](https://datatracker.ietf.org/doc/html/rfc6716#section-2)]. PCM adds none.
- Caveat on the micro-opus plain-ESP32 numbers: that board had quad PSRAM holding the pseudostack. The Echo would use internal RAM for it, which is likely faster, but that isn't measured [M/I].
- **Recommendation [I]:**
  - Use raw 16 kHz/16-bit PCM Streams on the LAN. 256 kbit/s is negligible against the ESP32's measured tens of Mbit/s of UDP throughput.
  - Keep ADPCM as a cheap option if bandwidth ever matters.
  - Don't use Opus on the Echo: it costs a lot of CPU and RAM and gains nothing on a LAN.

## 5. Sample rates, bit depths, and quality issues

**Mic (Sensor):**
- Format and rates:
  - PDM-to-PCM output is always 16-bit [M, ESP-IDF, ESPHome].
  - With `DSR_8S` (64×), fs can be about 15.6–50.8 kHz. With `DSR_16S` (128×), about 7.8–25.4 kHz. These ranges follow from the 1.0–3.25 MHz clock limit [I, arithmetic].
  - 16 kHz works in either mode. M5's example uses 16 kHz, PDM slot RIGHT ([RecordPlay.ino](https://github.com/m5stack/M5Atom/blob/master/examples/Echo/RecordPlay/RecordPlay.ino)).
- SPM1423 specs: SNR 61.5 dB(A), sensitivity −22 dBFS, THD 1% at 100 dB SPL [M, datasheet].
- DC offset: on one Echo, the noise floor was −30 dB before DC removal and about −60 dB after. ESPHome added `correct_dc_offset` for this [M, [esphome#8751](https://github.com/esphome/esphome/pull/8751)].
- Low level: ESPHome's stock config uses `gain_factor: 4`, `auto_gain: 31dBFS`, `noise_suppression_level: 2` [M]. Users boost gain up to 64 for far-field wake words and report "quality is rather bad compared to other diy mems microphones" [A, [esphome/issues#6059](https://github.com/esphome/issues/issues/6059)].

**Speaker (Actuator):**
- NS4168: 8–96 kHz with automatic rate detection, standard I2S. It picks the L or R channel by CTRL pin voltage. It has anti-clipping "NCN" AGC (10 ms attack, 1.1 s release) and outputs 2.5 W at 5 V/4 Ω [M, datasheet].
- On the Echo, use 16-bit **stereo** frames. ESPHome's config says: "The Echo has poor playback audio quality when using mono audio" [M, [yaml L55](https://github.com/esphome/wake-word-voice-assistants/blob/94dd3d17249bf391a1090cd3fd9cf189909605c3/m5stack-atom-echo/m5stack-atom-echo.yaml#L55)]. The maintainer adds: "tinny sound on an ATOM Echo … could not fix it at all, unless I just sent it stereo audio" [M, [esphome#16568](https://github.com/esphome/esphome/issues/16568)]. Root cause: in ESP32 16-bit mono mode, the data on the line is word-swapped [M, ESP-IDF I2S].
- Loudness: M5Unified applies ×12 software gain for the Echo speaker [M, [L3188](https://github.com/m5stack/M5Unified/blob/8b63555e7bd9ac71978c55ad868fd1d1d3b2ef3e/src/M5Unified.inl#L3188)]. One user called the built-in speaker "too silent" [A].
- M5 warns against DC output, long white noise, and full-scale square waves, because they risk damage [M].
- ESPHome plays back at 16 kHz with a 60 ms speaker buffer and a 6000 B media buffer, WAV only [M, yaml].

**Wi-Fi:** M5Unified drives GPIO0 high on Atom boards because "CH552 applies 4v to GPIO0, thus reducing WiFi sensitivity" [M, [L2359](https://github.com/m5stack/M5Unified/blob/8b63555e7bd9ac71978c55ad868fd1d1d3b2ef3e/src/M5Unified.inl#L2359-L2363)].

## 6. Latency notes (for the 150 ms / 200 ms targets)

- The Wi-Fi default is `WIFI_PS_MIN_MODEM`, where "the delay in receiving Wi-Fi data may be the same as the DTIM cycle". **Use `WIFI_PS_NONE`**, otherwise hub-to-speaker latency can blow the 200 ms budget by itself [M, ESP-IDF power-save doc]. The esp-libopus author saw data arrive in bursts with ~300 ms pauses [A].
- The mic DMA in ESPHome is 4 × 256 frames, which is 64 ms at 16 kHz [M]. For lower latency, use smaller DMA frames (for example 10–20 ms) [I].
- The mic-to-speaker switch costs a driver re-init plus ≤10 ms mic wake-up [M] plus DMA fill. This is **unmeasured**; measure it on hardware.

## 7. Alternatives if full duplex is needed

- **Atomic Voice/Echo Base (A149)**: ES8311 codec, MEMS mic with ≥65 dB SNR, NS4150B amp with a 1 W speaker. M5 lists it as **"Full-duplex"**. It mounts on the same ESP32-PICO-D4 Atom Lite/Matrix, so it has the same no-PSRAM RAM limits. xiaozhi supports it ([docs](https://docs.m5stack.com/en/atom/Atomic%20Echo%20Base)) [M].
- **Atom EchoS3R / "Atom VoiceS3R" (C126-ECHO)**: ESP32-S3-PICO-1-N8R8 with **8 MB PSRAM**, ES8311, NS4150B, and a 1 W speaker. It's the same 24×24 mm form factor and $14.50 in the M5 store. Audio pins: WS G3, BCLK G17, DOUT G48, DIN G4, MCLK G11, I2C G45/G0. Button G41, IR G47 ([docs](https://docs.m5stack.com/en/core/Atom_EchoS3R)) [M].

## 8. Open questions (need the real Device)

1. Measured free heap and largest block after Wi-Fi STA connect, and after a TLS connect, in a lean ESP-IDF build.
2. Mic↔speaker switch time on the Echo, both driver re-init and GPIO re-route variants.
3. Opus encode RTF on a plain ESP32 with esp_audio_codec vs micro-opus. This is only needed if Opus is ever reconsidered.
4. Does the NCN AGC in the NS4168 audibly pump on TTS?

## Sources

- M5Stack: [Atom Echo/Voice docs](https://docs.m5stack.com/en/atom/atomecho), [schematic](https://static-cdn.m5stack.com/resource/docs/products/atom/atomecho/atomecho_sch_01.webp), [Atomic Voice Base](https://docs.m5stack.com/en/atom/Atomic%20Echo%20Base), [Atom VoiceS3R](https://docs.m5stack.com/en/core/Atom_EchoS3R), [M5Unified @8b63555](https://github.com/m5stack/M5Unified/tree/8b63555e7bd9ac71978c55ad868fd1d1d3b2ef3e), [M5Atom RecordPlay](https://github.com/m5stack/M5Atom/blob/master/examples/Echo/RecordPlay/RecordPlay.ino)
- Datasheets: [SPM1423HM4H-B](https://m5stack.oss-cn-shenzhen.aliyuncs.com/resource/docs/datasheet/core/SPM1423HM4H-B_datasheet_en.pdf), [NS4168 (CN)](https://m5stack.oss-cn-shenzhen.aliyuncs.com/resource/docs/datasheet/core/NS4168_CN_datasheet.pdf), [ESP32-PICO series](https://m5stack-doc.oss-cn-shenzhen.aliyuncs.com/669/esp32-pico_series_datasheet_en.pdf)
- Espressif: [ESP-IDF I2S (ESP32)](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/i2s.html), [memory types](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/memory-types.html), [Wi-Fi performance & power save](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/wifi-driver/wifi-performance-and-power-save.html), [mbedTLS heap](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/protocols/mbedtls.html), [RAM usage guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/performance/ram-usage.html), [ESP-ADF design considerations](https://github.com/espressif/esp-adf/blob/master/docs/en/design-guide/design-considerations.rst), [esp_audio_codec README](https://github.com/espressif/esp-adf-libs/blob/master/esp_audio_codec/README.md)
- ESPHome: [Atom Echo config](https://github.com/esphome/wake-word-voice-assistants/blob/94dd3d17249bf391a1090cd3fd9cf189909605c3/m5stack-atom-echo/m5stack-atom-echo.yaml), [PR #99 (no duplex, stereo fix)](https://github.com/esphome/wake-word-voice-assistants/pull/99), [i2s_audio source @a7e0c0f](https://github.com/esphome/esphome/tree/a7e0c0f8a2308b74c7a3cdde14d2319ba18003d1/esphome/components/i2s_audio), [#8751 DC offset](https://github.com/esphome/esphome/pull/8751), [#16568 tinny mono](https://github.com/esphome/esphome/issues/16568), [#18271 IRAM LoadStoreError](https://github.com/esphome/esphome/issues/18271), [issues#6059 mic level](https://github.com/esphome/issues/issues/6059), [micro-opus benchmarks](https://github.com/esphome-libs/micro-opus)
- xiaozhi-esp32 @8ce50d2: [boards/m5stack](https://github.com/78/xiaozhi-esp32/tree/8ce50d27cd7c72c777673f46cbfc3ef7454d1b5c/main/boards/m5stack), [#1843 heap log (ESP32, no PSRAM)](https://github.com/78/xiaozhi-esp32/issues/1843)
- Community: [arduino-audio-tools #1742 (duplex attempts)](https://github.com/pschatzmann/arduino-audio-tools/discussions/1742), [XasWorks/esp-libopus](https://github.com/XasWorks/esp-libopus), [RFC 6716](https://datatracker.ietf.org/doc/html/rfc6716)
