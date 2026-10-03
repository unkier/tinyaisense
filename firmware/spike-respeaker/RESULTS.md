# ReSpeaker Lite full-duplex I2S spike: results, 2026-10-03

PROTOTYPE, throwaway: the answer to "Full-duplex I2S spike on the ReSpeaker" (#59). The unit is
a ReSpeaker Lite with a XIAO ESP32S3 and a 48 kHz I2S XU316 image, with the JST speaker. The
acoustic rig has the POD touching the board by the mics, the mouth 10–20 cm away aimed at the
mics, and the JST speaker ≤5 cm from the mics.

## Running it

- Firmware: `pio run -d firmware/spike-respeaker -t upload --upload-port /dev/ttyACM0`. First
  copy `src/secrets.h.example` to `src/secrets.h`; WiFi is only needed for `load`.
- The firmware is a console on the USB-Serial/JTAG. Type `help`; every command ends with
  `#done`.
- Host: from the repo root,
  `.venv/bin/python firmware/spike-respeaker/host/spike.py [--vol dB] [--gain dB] [--n N] [--lead L] [--flash] <exp> [args]`.
  - Experiments: calib, mic, aec, hf, vol, volaec, aecab, cutair, boot, romlog, listen,
    poweron, load.
  - `con <cmd>...` runs console commands.
  - WAVs and JSON go to `out/`, which is gitignored.
- pyserial must open the port with `dtr=True, rts=True`. A plain open passes through RTS=1,
  DTR=0, which resets the chip.

## Link

- The XU316 is the I2S master: WS 47998.6 Hz, BCLK 3.072 MHz, Philips, 32-bit stereo slots.
  It also drives MCLK at 24.59 MHz on GPIO9, so the ESP must never drive GPIO9.
- The ESP runs one I2S controller as a full-duplex slave: `i2s_new_channel(&cc, &tx, &rx)`.
- The DMA ring is 8 × 120 frames (2.5 ms). Hours of runs saw no queue overflows.
- Loop mode: GPIO43 is routed into RX in software (`esp_rom_gpio_connect_in_signal`), so RX
  reads TX bit-exactly.
  - TX↔RX frame offset K is constant within a boot, but differs per boot and per XU316
    reset: 0, 2, −1, 0. The slots stay aligned.
- A TX descriptor reaches the line 74–94 µs after its predecessor's `on_sent`.
- XU316 GETVERSION (`F0/D8`) reads **1.1.0**, not the 1.0.9 we were told.
- AIC3204 after XU316 boot:
  - P0 R27=0x30 (I2S, 32-bit, slave).
  - R56=2: the DAC input is the ESP's DOUT, bypassing the XU316.
  - R63=0xD4: soft-stepping.
  - R65/66=0 dB.

## Mic

- **Mouth → I2S: ~120 ms.** The round trip (mouth chirp → ch0, compared with ESP chirp → POD)
  measured 120.5 ms (sd 0.32, n=10). The speaker side is ~0.6 ms (DAC filter 21/fs plus ≤5 cm
  of air), which leaves ~120 ms for the mic. The ESP's own chirps reach ch0 120.4 ms after the
  line.
  - For comparison: the Echo's mouth→hub was 17–19 ms, and XVF3800's datasheet gives 58 ms.
- ch0 is band-limited by an FIR: −64 dB above 8.2 kHz, with no zero-order-hold images, so
  keeping every 3rd sample is clean.
  - Its low 16 bits are in use.
  - The silence floor is −46 to −50 dBFS.
- ch1 sits 18–25 dB under ch0 and is uncorrelated with it (0.017). The image may be MWW.

## Speaker

- **Playback bypasses the XU316.** A 12 kHz tone reaches the air (POD −20.9 dB, against −8.6 dB
  for 1 kHz), while ch0 at 12 kHz is −113 dBFS.
- I2S → air is ~0.6 ms: the AIC3204 filter, 21/fs = 0.44 ms, plus air.
- Ear: JST speech sounds **clean**.

## Full duplex: AEC, NS, AGC (speech; tones are invalid probes)

- The XU316's NS removes steady tones from any source, mouth or ESP, within ~0.5–1 s.
- Converged, the ESP's own speech in ch0 sits **31 dB under the same speech from the mouth**
  (≈34 dB at equal SPL), at the silence floor (−46 to −47 dBFS).
  - The first playback converged from −29 to −43 dBFS in ~4 s.
- **Convergence is slow and depends on history.** After a disturbance (e.g. AIC volume
  steps), the residual took ~1–2 min of own speech to recover: −26 → −41 dBFS over 7 × 12 s
  runs. Until then the residual sits 15–20 dB above converged.
- Sparse chirps are barely cancelled (match 0.95).
- Double talk keeps the near end: ch0 −13.5 dBFS against −13.7 for the mouth alone.
  ch0's envelope correlates 0.63 with the mouth's speech and 0.22 with the ESP's.
- WAVs: `aec_esp_A_again_ch0.wav`, `aec_double_talk_ch0.wav`, `aec_mouth_A_ch0.wav`.

## Volume

- AIC3204 DAC gain (P0 R65/66, 0.5 dB steps) and software gain in the 32-bit slots are both
  exact at the POD. 6 dB steps measured 6.1–6.3 dB, 0.5 dB steps 0.52 dB, and +24 dB measured
  +24.9 dB.
- Clicks (a 1 kHz tone, harmonics notched, >2 kHz over the floor):
  - AIC soft-stepping: −1…11 dB on 6 dB steps, 14 dB on +24 dB.
  - Unramped software gain: 7…21 dB on 6 dB steps, 20 dB on +24 dB.
  - A software ramp would fix the difference.
- **The AEC reference is the I2S TX, so volume must be applied before it.**
  - A −12 dB **AIC** step during speech raised the ch0 residual **+10…+24 dB** over the
    no-step control for 1–2 s and more, and the step back up did the same. This held in 4/4
    runs at three levels. It is mis-adaptation.
  - The same step in **software** lowered the residual 5–9 dB, with no spike.
  - So use software gain (ramped), and keep the AIC fixed at a calibrated maximum.

## Cut (tas_cut, unchanged)

- tas_cut runs as is with `channels = 4`: each 32-bit stereo frame is 4 s16 "channels", with the
  audio in the high halves. It uses `auto_clear_after_cb` and learns the ring from `on_sent`.
- **The DMA fetches ~19–20 frames (0.4 ms) ahead of the model.** In the lead scan, a lead of 18
  never plays and 20 always does, so fetch_lead must be ≥24; use 28. The TX FIFO holds ~24
  frames.
- Cut → silence (<−60 dBFS), 100 cuts per mode, lead 28, 2 ms ramp, 0 fails:
  - On the line (loop mode): min 2.58 / p50 3.26 / p95 4.36 / p99 4.59 ms.
  - tas_cut's model agrees within ~33 µs (p50 3.23, p99 4.56). In normal mode: p50 3.49,
    p99 4.58.
- At the air (POD, >2 kHz over the floor):
  - Ramped cut: 3.7–6.8 dB (p50 5.6). That is marginal against the HIL's 6 dB, where the Echo
    got 2.3.
  - Plain stop: 24 dB.
  - Ear: the cut is silent, the stop clicks.

## Boot

- XU316 reset (GPIO2, 1 ms pulse) → WS, I2C and the mic's first sample: **2.66 s** (n=6,
  ±1 ms), with no I2S queue overflows. A pop peaks at −42 dB at the POD, ~15 dB over the floor:
  small.
- ESP app_main starts ~644 ms after reset. ~430 ms of that is the PSRAM memtest.

## ROM log on GPIO43 (DOUT)

| event | POD peak | >20 dB over floor |
|---|---|---|
| soft reboot, ROM log on | −6.5 dB | 80 ms |
| soft reboot, `rtc_suppress_rom_log()` | −38.8 dB | 0 ms |
| USJ reset, ROM log on / suppressed | −6.4 / −36.4 dB | 73 / ~0 ms |
| reboot with the amp off first | −44.6 dB | 0 ms |
| power cycle (unplug, replug) | handling only | ear: silent |
| flash over USJ | −6.6 dB | 840 ms within 9 s |

- The suppress bit (RTC_CNTL_STORE4) survives soft, USJ and WDT resets, so setting it at boot
  silences every later reset.
- A power-on prints the ROM log too, but it is silent: the AIC isn't configured yet.
- Flashing is noisy, but only at dev time.

## Headroom (WiFi on, 16 kHz full duplex over UDP)

- Core 1 is 76% idle (the TX task takes 20.9%, with a naive 72-tap FIR). Core 0 is 92% idle.
- Internal heap: 92.8 KB free (min 89.4, largest block 31.7 KB). PSRAM: ~8.25 MB free.
  Spike buffers: 6 MB capture, 1.5 MB load.
- 401/401 frames, with no under- or overruns.
