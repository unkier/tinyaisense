# ReSpeaker Lite hardware facts for the port

Research for [#57](https://github.com/unkier/tinyaisense/issues/57), part of map [#56](https://github.com/unkier/tinyaisense/issues/56).
The target is a ReSpeaker Lite kit with a XIAO ESP32S3 pre-soldered. Its XU316 runs `respeaker_lite_i2s_dfu_firmware_48k_v1.0.9.bin`.
The firmware is ESP-IDF 5.5 C, built with PlatformIO (`espressif32@6.12.0` = IDF 5.5.0). This is desk research only: no hardware was touched.

Each fact carries a tag:

- **[V]** verified: a primary source states it.
- **[I]** inferred: derived from primary sources. The reasoning is given.
- **[U]** unverified: no primary source found. The fact is listed under "Measure in the spike" at the end.

## Sources

The links below are pinned to the commits that were read on 2026-10-03.

- **RL**: Seeed's [respeaker/ReSpeaker_Lite @2ad81e22e7](https://github.com/respeaker/ReSpeaker_Lite/tree/2ad81e22e7). It holds the README, the pinout and jack-detection images, `xmos_firmwares/` (the binaries, `changelog.md`, `dfu_guide.md`), and the Arduino examples.
- **SLN**: Seeed's [respeaker/sln_voice_respeaker_lite @1e37a2be28](https://github.com/respeaker/sln_voice_respeaker_lite/tree/1e37a2be28). This is XMOS sln_voice v2.x plus two Seeed commits from 2026-06-08 that add a `RESPEAKER_LITE` board to the FFVA example.
  - **It is not the source of the shipped I2S images.** Its I2S build makes the XU316 an I2S *slave* ([`ffva_int.cmake:9`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/ffva_int.cmake#L9)). It also builds the I2C control service out ([`platform_conf.h:105`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/bsp_config/RESPEAKER_LITE/platform/platform_conf.h#L105) `appconfI2C_DFU_ENABLED 0`). The shipped I2S images are I2S master and do have I2C control.
  - It is still the best view of the board wiring, the AIC3204 setup and the 48 kHz SRC.
- **FB**: [formatBCE/Respeaker-Lite-ESPHome-integration @4282ef33eb](https://github.com/formatBCE/Respeaker-Lite-ESPHome-integration/tree/4282ef33eb), with its patched mic driver [formatBCE/esphome `respeaker_microphone` @04a9f40154](https://github.com/formatBCE/esphome/blob/04a9f40154/esphome/components/i2s_audio/microphone/i2s_audio_microphone.cpp). It targets the 48k **v1.1.0** (MWW) image, not v1.0.9.
- **EH**: ESPHome's [`aic3204` component @06bd92c388](https://github.com/esphome/esphome/blob/06bd92c388/esphome/components/aic3204/aic3204.cpp). FB uses it.
- **VPE**: the Home Assistant Voice PE [`voice_kit.cpp`](https://github.com/esphome/home-assistant-voice-pe/blob/dev/esphome/components/voice_kit/voice_kit.cpp). It is a different board with the same XU316, and FB's `respeaker_lite.cpp` is copied from it.
- **TI**: the [TLV320AIC3204 datasheet SLOS602E](https://www.ti.com/lit/ds/symlink/tlv320aic3204.pdf) and [Application Reference Guide SLAA557](https://www.ti.com/lit/ml/slaa557/slaa557.pdf).
- **XMOS**:
  - [Device control (I2C) protocol](https://www.xmos.com/documentation/XM-014785-PC/html/modules/rtos/doc/programming_guide/reference/rtos_services/device_control/device_control_protocol.html)
  - [XVF3800 datasheet, pipeline](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html)
  - [lib_src fixed-ratio SRC](https://github.com/xmos/lib_src/blob/develop/doc/rst/fixed_ratio_src.rst) and [`src_ff3v_fir.h`](https://github.com/xmos/lib_src/blob/develop/lib_src/src/fixed_factor_of_3_voice/src_ff3v_fir.h)
- **IDF**: ESP-IDF **tag v5.5**, which is byte-identical to the 5.5.0 that PlatformIO installs. The `release/v5.5` head has since rewritten the full-duplex code. The pages read:
  - [i2s.rst](https://github.com/espressif/esp-idf/blob/v5.5/docs/en/api-reference/peripherals/i2s.rst)
  - [`i2s_common.c`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_driver_i2s/i2s_common.c), [`i2s_common.h`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_driver_i2s/include/driver/i2s_common.h), [`i2s_std.c`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_driver_i2s/i2s_std.c) and [`i2s_std.h`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_driver_i2s/include/driver/i2s_std.h)
  - [usb-serial-jtag-console.rst](https://github.com/espressif/esp-idf/blob/v5.5/docs/en/api-guides/usb-serial-jtag-console.rst)
  - [`esp_rom/Kconfig.projbuild`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_rom/Kconfig.projbuild) and [`rom/rtc.h`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_rom/esp32s3/include/esp32s3/rom/rtc.h)
- **TRM**: the [ESP32-S3 Technical Reference Manual](https://www.espressif.com/sites/default/files/documentation/esp32-s3_technical_reference_manual_en.pdf) v1.8. Chapters used: 6 IO MUX, 7 Reset, 8 Strapping, 28 I2S, 33 USB-Serial/JTAG.
- **esptool**: [v4.9.0 `reset.py`](https://github.com/espressif/esptool/blob/v4.9.0/esptool/reset.py) and [`loader.py`](https://github.com/espressif/esptool/blob/v4.9.0/esptool/loader.py), plus the [boot-mode docs](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/advanced-topics/boot-mode-selection.html).
- **Seeed wiki**: the source lives in Seeed-Studio/wiki-documents, branch `docusaurus-version`.
  - [reSpeaker Lite intro](https://wiki.seeedstudio.com/reSpeaker_usb_v3/), [kit page](https://wiki.seeedstudio.com/xiao_respeaker/) and [I2S test](https://wiki.seeedstudio.com/respeaker_i2s_test/)
  - [XIAO ESP32S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) and the [arduino-esp32 XIAO_ESP32S3 pin map](https://github.com/espressif/arduino-esp32/blob/master/variants/XIAO_ESP32S3/pins_arduino.h)
- **Forum**: Seeed threads [281743](https://forum.seeedstudio.com/t/respeaker-lite-on-raspberry-using-i2s/281743) (I2S roles), [283114](https://forum.seeedstudio.com/t/respeaker-lite-aec-support/283114) (AEC) and [280622](https://forum.seeedstudio.com/t/respeaker-lite-voice-assistant-kit-schematic/280622) (schematic).

**No full schematic is published.** RL has only the pinout and the jack-detection circuit. Forum 280622 confirms that nothing else exists. Wiring behind the XIAO header is therefore inferred from firmware register settings.

## 1. Board and MCU

- **[V]** Kit SKU 110061601 has a XIAO ESP32S3 that is "pre-soldered via the I2S pins" (kit wiki).
  - The XIAO uses an ESP32-S3R8 with 8 MB PSRAM and 8 MB flash (XIAO wiki). The PSRAM is octal: FB sets `psram: mode: octal`.
- **[V]** The XIAO's USB-C is the S3's native USB-Serial/JTAG, 303a:1001. esptool treats PID 0x1001 as USB-JTAG-Serial (`loader.py:290`).
  - The board's own USB-C belongs to the XU316. The USB image enumerates there as UAC2. Every image offers DFU there (RL `dfu_guide.md`).
- **[V] New:** the XIAO itself has a **user LED on GPIO21** and a **BOOT button on GPIO0**. A RESET button and a charge LED are also on the XIAO (XIAO wiki pin table; arduino-esp32 `LED_BUILTIN = 21`).
  - These matter for #60, which assumes the board has no LED and no button.
  - **[U]** The LED's polarity, and whether both are reachable once mounted, are not checked.

## 2. I2S link

### Pins

**[V]** From the RL `pinout.png` labels, with XIAO D-pins mapped to GPIOs by arduino-esp32 `pins_arduino.h`:

| XIAO pin | GPIO | ReSpeaker net | Use |
|---|---|---|---|
| D8 | 7 | I2S_LRCLK | WS, an input to the ESP |
| D9 | 8 | I2S_BCLK | BCLK, an input to the ESP |
| D6 | 43 | I2S_DIN_SEC (dout) | ESP DOUT, playback (also UART0 TX) |
| D7 | 44 | I2S_DIN_XIAO (din) | ESP DIN, mic (also UART0 RX) |
| D10 | 9 | I2S_MCLK | do not drive (see "MCLK") |
| D4 / D5 | 5 / 6 | SDA / SCL | I2C |
| D1 | 2 | XU316_RST | XU316 reset |
| D0 | 1 | XIAO_WS2812 | RGB LED, shared with the XU316's X0D30 |
| D2 / D3 | 3 / 4 | XIAO_D2 / D3 | free; solder pads to USR / MUTE |

### Roles

- **[V]** The XU316 is the I2S master and the host is the slave.
  - Seeed staff (jiewei.ye, forum 281743, 2024-10-14): "ReSpeaker Lite works as an I2S master and provides the clock … slave, with 16KHz+2channels+32bit format".
  - Seeed's own Arduino examples use `is_master = false` with `AudioInfo(16000, 2, 32)` (RL `xiao_esp32s3_arduino_examples/*`).
  - FB uses `i2s_mode: secondary` at 48 kHz, 32-bit, stereo.
- **Conflict resolved:** the wiki's [I2S test](https://wiki.seeedstudio.com/respeaker_i2s_test/) calls `I2S.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_16BIT, …)`. ESP_I2S's `begin` defaults to the master role, so this is a documentation error.
  - The test only counts non-zero RX samples, so it can "pass" while both sides fight over the clock lines.
  - SLN's slave build is an unshipped variant (see Sources), so it does not count against the master role.

### Format

- **[V]** Stereo with 32-bit slots, in Philips (I2S) framing.
  - The AIC3204 that shares the bus is programmed with `CODEC_IF (P0_R27) = 0x30`, which TI defines as I2S framing, 32-bit words, and BCLK/WCLK as inputs (SLAA557 §5.2.27; SLN `aic3204.c`; EH).
  - The rate is fixed by the image: 48 kHz for ours.
  - The ESP-side config is `I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO)`. That gives a 32-bit WS width with a 1-bit shift (IDF `i2s_std.h:140-151`).

### Channel layout (mic, ESP RX)

- **[V]** In the MWW v1.1.0 images, ch0 is tuned for ASR and ch1 for Micro Wake Word (RL README).
  - FB feeds `voice_assistant` from channel 0 and `micro_wake_word` from channel 1 (FB base YAML lines 1478-1482 and 1360-1365).
- **[I]** In v1.0.9, ch0 is the processed mic: AEC, then IC + VNR, then NS, then AGC.
  - In the sln_voice FFVA pipeline that the image derives from, `samples[0]` carries the AGC output. `samples[1]` is left as the AEC'd second mic after IC ("Intentionally ignoring comms ch from here on out", SLN [`fixed_delay/audio_pipeline_t0.c:83`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/modules/audio_pipelines/reference/fixed_delay/audio_pipeline_t0.c#L83)).
  - The I2S output sends `samples[0]` and `samples[1]` (SLN `main.c` slave path).
- **[U]** What ch1 carries in v1.0.9 is not stated anywhere for the shipped image. Use **ch0 only**.

### Playback (ESP TX) goes straight to the DAC, not through the XU316

- **[V]** In I2S firmware the AIC3204 takes DAC data from its **SCLK/MFP3 pin as the secondary data input**, while BCLK and WCLK stay primary.
  - The registers are `P0_R56 = 0x02`, `P0_R31 = 0x01` and `P0_R32 = 0x01`. TI defines them as "SCLK enabled for Secondary Data Input", "Secondary Data Input = SCLK" and "Secondary Data In is used for Audio Data In" (SLAA557 §5.2.31, §5.2.32, §5.2.53).
  - Both sources set these, and only for I2S builds:
    - EH: "For I2S Firmware only, set SCLK/MFP3 pin as Audio Data In" ([`aic3204.cpp:40`](https://github.com/esphome/esphome/blob/06bd92c388/esphome/components/aic3204/aic3204.cpp#L40)).
    - SLN: `#if appconfUSB_ENABLED … #else 0x38=0x02, 0x1F=0x01, 0x20=0x01` in [`aic3204.c`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/bsp_config/dac/aic3204/aic3204.c#L36-L44).
- **[I]** That secondary input is the ESP's GPIO43, via the net labelled I2S_DIN_SEC (dout). Two pieces of evidence:
  - It is the only I2S data net besides the mic line.
  - FB warns that a UART0 logger "introduces noise on speaker" (FB base YAML line 111).
- **Consequence 1:** the XU316 pipeline is **not** in the playback path. The XU316 only taps the same line as its AEC reference (`appconfAEC_REF_I2S`, SLN `ffva_int.cmake`).
- **Consequence 2:** anything on GPIO43 is played by the speaker. That includes ROM log bytes, UART traffic and stale DMA buffers.
- **Consequence 3:** the DAC runs at the I2S rate, so playback is 48 kHz on our image.

### MCLK

- **[I]** Leave GPIO9 unconfigured, with `.mclk = I2S_GPIO_UNUSED`.
  - Slave mode does not need it: IDF `i2s_check_set_mclk` returns early when it is unused (`i2s_common.c:926-927`).
  - An assigned MCLK pin becomes an **output** unless `clk_src = I2S_CLK_SRC_EXTERNAL` (`i2s_std.h:277`).
- **[I]** The net probably carries the XU316's 24.576 MHz codec clock.
  - The AIC3204 clocks from its MCLK pin. EH says "24.576 MHz when the XMOS is expecting 48kHz audio" and divides by NDAC 2 × MDAC 2 × DOSR 128 to get 48 kHz.
  - SLN uses MDAC 6 for 16 kHz.
- **[U]** FB sets `i2s_mclk_pin: GPIO9`, which may mean the ESP drives against the XU316. Why that is harmless for FB is unknown.

### Full duplex on one controller (IDF 5.5)

- **[V]** One controller can do slave full duplex: `i2s_new_channel(&cfg, &tx, &rx)` with `I2S_ROLE_SLAVE`.
  - The driver marks the channel full-duplex and shares BCK/WS, routing both to the RX slave inputs (`i2s_common.c:1001-1027`, `i2s_std.c:127-128, 181-192`).
  - The docs say TX and RX "have to be same in full-duplex mode" (`i2s.rst`). 5.5.0 does not check this in code.
  - The header (`i2s_common.h:109`) says TX/RX read and write are not synchronised; only the clocks are shared.
- **[V]** FB instead uses **two controllers**, one RX and one TX, both secondary, sharing GPIO7/8. That is also valid.
- **[V]** In slave mode `sample_rate_hz` only sizes the internal clock. The driver sets `mclk = bclk × bclk_div` with `bclk_div ≥ 8` (`i2s_std.c:45-53`). The TRM requires `f_I2S_CLK ≥ 8·f_BCK` in slave mode (TRM §28.6).
  - At 48 kHz, stereo, 32-bit: BCLK is 3.072 MHz and the internal clock is 24.576 MHz from PLL_F160M.
  - The `bclk_div` field note reads: "increase … when data sent by slave lag behind" (`i2s_std.h:270`).
- **[V]** When the master's clock stops, no DMA EOF arrives and no callbacks fire. `i2s_channel_read` and `i2s_channel_write` return `ESP_ERR_TIMEOUT` with partial byte counts (`i2s_common.c:1322, 1367`).
  - A slave TX with no new data repeats or zeroes the FIFO, depending on hardware (TRM §28.8.1).
  - **[U]** How a slave realigns when the clock restarts, after the XU316 resets.

### DMA patching caveats (`tas_cut`)

- **[V] The `auto_clear` flags behave opposite to their header comments in v5.5.**
  - In `i2s_dma_tx_callback`, `auto_clear_before_cb` memsets the finished buffer *before* calling `on_sent`, and `auto_clear_after_cb` memsets it *after* `on_sent` ([`i2s_common.c:615-657`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_driver_i2s/i2s_common.c#L615-L657)).
  - So anything that `on_sent` writes into `dma_buf` is wiped under `after_cb`, which is what the header claims to protect.
  - `auto_clear` is an alias of `auto_clear_after_cb`. With neither flag set, an idle ring **replays stale audio**, and here that reaches the speaker directly.
  - The Echo's `tas_cut` uses `auto_clear_after_cb` and writes ring buffers from the task, not from the callback, so it is unaffected. The same driver applies to the S3.
- **[V]** `on_sent` gives `dma_buf`, the buffer that just finished, and `size`, the buffer size. Writing to `dma_buf` in the ISR is allowed. Under `on_send_q_ovf`, `dma_buf` is NULL.
- **[V]** The ring is circular. Each buffer is `dma_frame_num × slots × 4 B`, which is 8 B per frame at 32-bit stereo.
  - The cap is 4092 B per descriptor (`DMA_DESCRIPTOR_BUFFER_MAX_SIZE_4B_ALIGNED`), so **≤ 511 frames** per descriptor. The minimum is 2 descriptors.
  - The buffers are in internal RAM, so the S3 needs no cache sync.
- **[V]** `i2s_channel_preload_data` works only before `i2s_channel_enable`, and fills from `desc[0]`. Enable resets the FIFO and DMA (`i2s_common.c:120-140, 1246-1300`).
- **[I]** TX latency is about (desc_num − 1 … desc_num) × frame_num / 48000, plus the 64-word TX FIFO (≈ 0.67 ms at 48 kHz, 32-bit stereo). Use `CONFIG_I2S_ISR_IRAM_SAFE` to keep the ISR running during flash writes.
- **[U]** The S3's GDMA prefetch lead before `on_sent` (the Echo's `fetch_lead`) at 32-bit slots.

## 3. 48 kHz in the 48k image

- **[V]** The pipeline runs at 16 kHz.
  - The wiki's spec table says "Maximum Sampling Rate 16Khz".
  - In sln_voice, `appconfAUDIO_PIPELINE_SAMPLE_RATE 16000`, and the frame is `MIC_ARRAY_CONFIG_SAMPLES_PER_FRAME=240`, i.e. 15 ms (SLN `app_conf.h:46`, [`RESPEAKER_LITE.cmake:43`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/bsp_config/RESPEAKER_LITE/RESPEAKER_LITE.cmake#L43)).
- **[I] Mic 16→48 kHz is a filtered upsample, not repeated samples.**
  - When `I2S rate == 3 × pipeline rate`, sln_voice installs I2S filter callbacks that use lib_src's **voice polyphase FIR**: `src_us3_voice_*` for TX to the host and `src_ds3_voice_*` for RX from the host ([`main.c:256-334`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/src/main.c#L256-L334), `platform_start.c` `i2s_rate_conversion_enable`).
  - The FIR has 72 taps: 3 phases × 24 (`src_ff3v_fir.h`), and lib_src describes it as a "72 tap Remez FIR" with about 60 dB SNR.
  - Its linear-phase group delay is (72 − 1)/2 = 35.5 samples at 48 kHz, **≈ 0.74 ms** per direction.
  - The shipped binary's source is closed and its image is encrypted or compressed (`strings` finds nothing), so this is inferred from the code it derives from.
- **[V]** FB keeps **every third stereo frame**, 8 B of each 24 B block, with no filter ([`i2s_audio_microphone.cpp:239-272`](https://github.com/formatBCE/esphome/blob/04a9f40154/esphome/components/i2s_audio/microphone/i2s_audio_microphone.cpp#L239-L272)).
  - **[I]** That works because the 48 kHz signal is band-limited below 8 kHz by the upsampling FIR. It recovers 16 kHz audio without aliasing, but the kept samples are FIR phase outputs, not the original 16 kHz samples.
- **[I] Playback:** the ESP's 48 kHz TX plays on the AIC3204 at 48 kHz directly (section 2). The XU316 down-samples the same line by 3 with the voice FIR to get the 16 kHz AEC reference (`src_ds3_voice`).
  - So playback has no 8 kHz band limit on the speaker, but the AEC only models the content below 8 kHz.
- **[V]** The DAC rate is set by the AIC3204 dividers and must match WCLK: 24.576 MHz / (NDAC·MDAC·DOSR). That is 2·2·128 for 48 kHz in EH and 2·6·128 for 16 kHz in SLN.
  - DAC filter A (PRB_P1) group delay is **21/fs**: 0.44 ms at 48 kHz, 1.3 ms at 16 kHz (SLAA557 Table 2-19).

## 4. XU316 boot

- **[V]** FB and VPE pulse the reset pin high for 1 ms, then hold it low, then wait **3000 ms** before the first I2C read ([`respeaker_lite.cpp:22-26`](https://github.com/formatBCE/Respeaker-Lite-ESPHome-integration/blob/4282ef33eb/esphome/components/respeaker_lite/respeaker_lite.cpp#L22-L26)).
  - FB configures `reset_pin: GPIO2` without `inverted`, so high is reset.
  - The 3 s comes from VPE's code. Nobody measured it on the ReSpeaker.
  - **[I]** The reset is active-high at GPIO2, probably through a transistor. A Raspberry Pi user had to ground XU316_RST to make the board run (forum 281743).
- **[I]** Expect the I2S clock about **2.5–3 s** after reset or power-on. In SLN's start order, the I2S clock and the I2C slave come up only after the codec is set up, and that setup waits:
  1. GPIO start, with the amp disabled.
  2. I2C master start.
  3. AIC3204 init: 10 ms reset, 10 ms, then a **2500 ms soft-step wait**.
  4. Mics.
  5. **I2S start.**
  6. Amp enable.
  7. **I2C slave start.**

  The source is [`platform_start.c:176-194`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/bsp_config/RESPEAKER_LITE/platform/platform_start.c#L176-L194) and [`aic3204.c:138`](https://github.com/respeaker/sln_voice_respeaker_lite/blob/1e37a2be28/examples/ffva/bsp_config/dac/aic3204/aic3204.c#L138).
  - The 2.5 s is a wait coded into the XMOS example, not a chip requirement. EH has the same wait.
  - **[U]** The real figure for the shipped image.
- **[I]** When the ESP does not feed TX, the XU316 is master and keeps clocking. The DAC and the AEC reference then see whatever is on GPIO43:
  - zeros, with `auto_clear` and an idle ring;
  - stale buffers, with no clear;
  - idle-high UART, i.e. −1 LSB, which is silent, before I2S takes the pin;
  - ROM log bytes, heard as noise.
- **[V]** An ESP reset does not reset the XU316. Only GPIO2 or a power cycle does.

## 5. I2C

- **[V]** The bus is GPIO5 (SDA, D4) and GPIO6 (SCL, D5). FB runs it at 400 kHz.
  - The XU316 is at **0x42**; this is the I2S firmware only, as the USB image has no I2C service.
  - The AIC3204 is at **0x18** (TI: address 0011000).
  - All three chips share the bus (RL README).
- **[V] Framing** follows the XMOS command transport protocol:
  - A write is `[resid, cmd, len, data…]`.
  - A read is a write of `[resid, cmd|0x80, n+1]`, then a read of n+1 bytes. The first byte is the status: 0 = done, 1 = wait (retry), 3 = invalid.
  - Multi-byte values are big-endian.
  - "GET commands are serviced by the end of each 15 ms audio frame", so a read can take up to about 15 ms or return WAIT.
- **[V] Commands** (RL README register map, RL Arduino examples, FB):

| RESID / cmd | Meaning | Since |
|---|---|---|
| `0xF1 / 0x80` (read 1) | VNR 0–100 | I2S v1.0.5 |
| `0xF1 / 0x81` (read 1) | mic-mute status (0/1) | I2S v1.0.9 |
| `0xF1 / 0x10` (write 1) | speaker enable: drives AUDIO_PA_EN, default 1 | I2S v1.0.7 |
| `0xF0 / 0xD8` (read 3) | firmware version major.minor.patch; this is the DFU servicer's GETVERSION, 0x58 \| 0x80 | — |
| `0xF0 / …` | DFU (section 6) | — |

  - **No AEC, AGC, NS or volume command is documented** [V].
  - The changelog for v1.0.7 says "support i2c control speaker mute **and output channels**", but no output-channel command is documented anywhere [U].
- **[U] The rate is not in the version.** Both the 16k and the 48k I2S images are labelled v1.0.9, and probably both report 1.0.9. The ESP likely cannot tell the rate from the version.
  - **[I]** Detect the rate by measuring WS, or the RX DMA EOF interval, instead.
- **[V] AIC3204 volume:**
  - **Use the DAC digital volume**: page 0 registers **0x41/0x42** (65/66), from +24 dB to −63.5 dB in 0.5 dB steps. Writes are soft-stepped one step per word clock when `P0_R63 = 0xD4`.
  - Mute is page 0 register 0x40 (64), bits D3:D2.
  - EH maps volume 0..1 onto bytes −127..48 this way.
  - The driver gains on page 1 registers 0x10–0x13 cover HPL/HPR and LOL/LOR from −6 to +29 dB in 1 dB steps, with D6 = mute. Seeed's Arduino volume example uses them. TI says these "are not meant to be used as dynamic volume control" (SLOS602E §9.3.4).
  - **This corrects the seed.**
  - The I2S image v1.0.9 changed the "default DAC output gain(volume) from 0dB to −2dB" (changelog).
- **[I] Bus sharing:** the XU316 masters the bus only during its boot, while it programs the AIC3204. After that it starts as a slave: SLN says "I2C slave can be started only after i2c_master_start() is completed".
  - So the ESP must stay off the bus for about 3 s after any XU316 reset.
  - After that the ESP can be the only master and may re-program the AIC3204, as EH does with a full re-init and its own 2.5 s wait.
  - **[U]** Whether the shipped image touches the AIC3204 later, for example through the "output channels" command.

## 6. XU316 firmware delivery

- **[V] Images** (RL `xmos_firmwares/`, MD5 from the files):
  - USB v2.0.7.
  - I2S 16k: v1.0.8 and v1.0.9.
  - **I2S 48k v1.0.9: 274,432 B, MD5 `f29e6229a4f6ca5c80553aa79fbbef0e`.**
  - I2S 48k MWW v1.1.0, MD5 `9297155d…`, the same file FB embeds.
  - I2S 16k MWW v1.1.0, named `…i2s_factory_firmware…`.
  - The README calls the 48k image "device name is 'ReSpeaker Lite(48K)'". That is a USB product string, which shows that the I2S images also enumerate on USB for DFU.
- **[V] DFU over USB:** `dfu-util -R -e -a 1 -D <bin>` on the XU316's USB-C, which is next to the 3.5 mm jack.
  - The device is 2886:0019. Its alt settings are 0 "DFU FACTORY", 1 "DFU UPGRADE" and 2 "DFU DATAPARTITION" (RL `dfu_guide.md`). The wiki's Linux steps use `sudo`.
  - **[I] On Linux this needs root once**, for a udev rule granting access to 2886:0019 (`TAG+="uaccess"` or `MODE`).
  - This PC's `99-platformio-udev.rules` covers 303a:1001 and 2886:802d/002d, but not 2886:0019. So USB DFU **violates the no-root rule** unless the owner installs a rule.
- **[V] DFU over I2C from the ESP** needs no root. FB `respeaker_lite.cpp`/`.h`, copied from VPE, does:
  1. `SETALTERNATE (0x40)`, payload 1 = upgrade.
  2. Repeated `DNLOAD (cmd 1)`: write `[0xF0, 0x01, 130, n, 0, data[128]]`, i.e. 128 B blocks.
  3. Before each block, poll `GETSTATUS (0x03|0x80)`. It returns 6 bytes: status, dfu_status, a 24-bit poll delay in ms, and the state. Proceed when the state is dfuIDLE, DNLOAD_IDLE or MANIFEST_WAIT_RESET.
  4. An empty DNLOAD ends the transfer.
  5. `REBOOT (0x59)`.
  6. Read GETVERSION to verify.

  The timeout is 4000 ms between ready states.
- **[U] Flashing time.** No source states it. **[I]** Estimate: 2,144 blocks at about 3 ms each on a 400 kHz bus, plus erase and program delays. Expect roughly 10–40 s (FB sends one block per ESPHome loop).
- **[I]** Any `*_dfu_firmware_*` image can be flashed over I2C, since FB flashes the 48k MWW one this way.
  - Flashing the **USB** image over I2C removes the I2C service, and recovery then needs USB DFU, i.e. root.
  - DFU writes the *upgrade* partition and leaves the factory image in place.
  - **[U]** Which image our unit's factory partition holds. A failed upgrade may fall back to it.
- **Conflict "which image ships by default", resolved:** no primary source states what kit 110061601 ships with.
  - The wiki's "(default firmware)" line refers to the USB image of the standalone board.
  - The kit page says only "make sure the ReSpeaker Lite's firmware is the I2S version".
  - FB does not say I2S ships. It tells you to flash 48k ≥ 1.1.0.
  - This is moot for us: our unit runs 48k v1.0.9, and the ESP can check the version over I2C. A failed I2C read means USB firmware, a dead XU316, or a read during the boot window.

## 7. XIAO USB-Serial/JTAG: flash, console, reset, ROM log

### Console

- **[V]** Set `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` and `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y` ([`esp_system/Kconfig:243-345`](https://github.com/espressif/esp-idf/blob/v5.5/components/esp_system/Kconfig)). There is no `ESP_CONSOLE_UART_NONE`.
  - The app then gates UART0's clock and does not touch GPIO43/44.
  - The I2S driver routes GPIO43/44 through the GPIO matrix and only warns if they are already reserved. The console does not reserve them (`i2s_common.c:869-911`).

### Reset and flash without root

- **[V]** The USJ peripheral maps CDC line states to actions (TRM Table 33.3-2):
  - RTS=1, DTR=0: **chip reset**.
  - RTS=0, DTR=1: set the download flag.
  - RTS=0, DTR=0: clear it.
  - RTS=1, DTR=1: no action.
- **[V]** esptool picks `USBJTAGSerialReset` for PID 0x1001, and `--after hard_reset` pulses RTS (`reset.py:124-165`).
  - This reset is a **core reset**: straps are not re-sampled and the RTC domain is kept (TRM Table 7.1-1).
  - The Echo's CP210x-style bridge drives EN and GPIO0 through two transistors, which gives a full chip reset.
- **[I]** A plain port open does not reset the chip, because Linux cdc-acm raises DTR and RTS together. pio monitor with `monitor_dtr=0` and `monitor_rts=0` should reset it.
- **[V] No root:** the port is `/dev/ttyACM0`, group `uucp` on Arch. On this PC it is also mode 0666 through PlatformIO's udev rule for 303a:1001, and the user is in `uucp`.
- **[V] Caveats:**
  - The port vanishes if the app reconfigures GPIO19/20, disables USJ, or deep-sleeps. Recovery is BOOT + RESET, by hand.
  - Without a reader, the console stalls once for 50 ms when its buffer fills.
  - After a manual entry into download mode, use `--after watchdog_reset`.
  - **[U]** Whether the port re-enumerates on a core reset. esptool retries the open.

### ROM log on GPIO43

- **[V]** By default the ROM prints to UART0 **and** USJ (TRM §8.3).
  - GPIO43 resets as U0TXD with output enabled and a pull-up, so it idles high and carries the ROM banner until I2S claims it (TRM Table 6.12-1).
  - **In download mode the ROM always prints on UART0**, whatever the eFuse or GPIO46 says (TRM Table 8.3-1). esptool enters download mode on every flash, **and `esptool run` does too**.
- **[V]** The 2nd-stage bootloader switches the ROM console to USJ "to avoid output on UART" when USJ is the console (`bootloader_console.c:120-131`). `CONFIG_BOOTLOADER_LOG_LEVEL_NONE` also exists.
- **[V]** Ways to silence it:
  - **Permanent:** `CONFIG_BOOT_ROM_LOG_ALWAYS_OFF`, or `…_ON_GPIO_HIGH`/`LOW`, which **burn the `UART_PRINT_CONTROL` eFuse once**.
    - GPIO46 has a pull-down and is not on the XIAO header, so `ON_GPIO_HIGH` is effectively off.
    - Download mode still prints.
  - **Temporary, no eFuse:** `rtc_suppress_rom_log()` (`rom/rtc.h:210`) sets `RTC_CNTL_STORE4_REG[0]`.
    - This survives core, software, watchdog-core and USJ (esptool) resets.
    - Power-on, brown-out and system resets clear it (TRM Table 7.1-1).
- **[I]** Calling `rtc_suppress_rom_log()` early in the app silences every later soft or USJ reset.
  - The first power-on banner falls inside the XU316's own boot window. There the amp is still disabled (SLN `gpio_start` → `amplifier_disable`) and the AIC3204 is not yet set up, so that banner is probably inaudible [U].
  - Flashing (download mode) stays audible unless the speaker is muted over I2C (`F1/10 = 0`) before the reset into the bootloader.

## 8. Latency and AEC

| Path | Figure | Tag |
|---|---|---|
| I2S → air (board part) | AIC3204 filter A 21/fs = **0.44 ms** at 48 kHz, plus the amp. The XU316 is not in the path. ESP DMA lead and the TX FIFO (≈0.67 ms) come on top. | [V] for the filter; [I] for the path |
| XU316 48↔16 SRC | 72-tap FIR, ≈ **0.74 ms** each way | [I] |
| Mic → I2S | **Not published** for the ReSpeaker Lite or sln_voice FFVA. Structural floor: one 240-sample frame (**15 ms**), plus PDM decimation, plus up to 5 pipelined stage hops (AEC on tile 1; IC+VNR, NS and AGC on tile 0), plus SRC and I2S buffering. The XVF3800 sibling (16 kHz pipeline with beamforming) states **"min 58 ms, Microphone In to I2S out"**. | [V] the frame size and the XVF3800 figure; [U] our figure |
| AEC | Tail and bulk delay for FFVA are not published. The XVF3800 states a 192 ms tail. FFVA's `fixed_delay` pipeline uses `appconfINPUT_SAMPLES_MIC_DELAY_MS 0`. | [V] |

- **[I] AEC alignment is causal.** The XU316 reads the reference at the same instant the DAC receives it, and the air path adds about 0.5 ms plus the acoustic delay.
- **[V] AEC quality reports** (forum 283114, mostly about the USB image):
  - "Isn't working out of the box".
  - "No AEC applied to channel 0".
  - Seeed staff: "sometimes the AEC filter does not work well", and the result depends on reverberation, speaker volume and position.
  - Seeed published a debug USB image, `ffva_ua_v2.0.6_output_proc0_ref0.bin`, with the processed mic on ch0 and the AEC reference on ch1.
  - A report says that a rate mismatch between host and image gives loud static or half-speed audio (forum 281743).

## Seed verdicts

| Seed claim | Verdict |
|---|---|
| SKU 110061601, S3R8, 8 MB flash, 8 MB octal PSRAM | **Verified.** Also: user LED GPIO21 and BOOT button GPIO0 on the XIAO. |
| Pipeline IC/AEC/NS/VNR/AGC, no beamforming; the image list | **Verified.** The list adds 16k v1.0.8 and the 16k MWW "factory" v1.1.0 file. |
| Which image ships by default | **Resolved:** no source states it for the kit; moot for us. |
| USB DFU command, 2886:0019, `sudo` | **Verified.** It needs a one-time root udev rule, so it conflicts with no-root. |
| I2C DFU (RESID 0xF0, 128 B) | **Verified.** Protocol detailed in section 6. |
| I2C pins, 0x42 framing, commands, AIC3204 0x18 | **Verified.** Additions: the "output channels" command is undocumented, and `F0/D8` is DFU GETVERSION. |
| Volume = page 1 regs 0x10–0x13 | **Corrected.** Use the DAC digital volume, page 0 regs 0x41/0x42, in 0.5 dB steps. |
| XU316 masters the bus at boot | **Verified in SLN**; inferred for the shipped image. |
| I2S pins; XU316 master; ESP slave | **Verified.** The wiki's ESP-master 16-bit example is a doc error. |
| sln_voice_respeaker_lite builds XU316 as a 48k slave | **Verified**, and it also lacks I2C control, so it is not the shipped source. |
| Philips, 32-bit, stereo, rate fixed by image | **Verified.** |
| ch0 is the processed mic (MWW: ch1 is wake word) | **Verified.** v1.0.9's ch1 is unknown. |
| 16 kHz pipeline in the 48k image; formatBCE keeps every 3rd frame | **Verified.** The upsample is a 72-tap FIR, inferred. |
| Playback is the AEC reference | **Verified in SLN.** Playback also feeds the DAC **directly**, bypassing the XU316. |
| AIC3204 LOL/LOR → amp (AUDIO_PA_EN) → JST "5 W"; the jack mutes the JST | **Verified.** The schematic shows MICDET pulling AMP_CTL low. |
| WS2812 GPIO1 (also XU316), reset GPIO2, GPIO3/4 free | **Verified.** The reset is active-high, inferred. GPIO3 is an S3 strapping pin, which only matters with JTAG eFuses. |
| Console on USJ; UART0 noise on the speaker | **Verified.** The cause: GPIO43 feeds the DAC. |
| 3 s wait; 2.5 s soft-start | **Verified as code waits** copied from VPE and the XMOS example. Not measured on this board. |
| No latency published; 15 ms frames; XVF3800 58 ms | **Verified.** |
| Seeed sample code is Arduino-only; formatBCE is the IDF-side reference | **Verified.** formatBCE uses the v1.1.0 MWW image, not v1.0.9. |
| XIAO port = S3 USJ 303a:1001; board port = XU316 | **Verified.** |

## Measure in the spike (#59)

1. Time from reset (GPIO2) and from power-on to a running BCLK/WS, and to I2C answering.
2. v1.0.9's ch1 content, by correlating it with ch0 and the raw mics. Also check that ch0 at 48 kHz is band-limited (FIR), not triplets (ZOH).
3. Mic → I2S latency, against the 15 ms floor and the XVF3800's 58 ms.
4. I2S → air latency. Expect about 1 ms plus the DMA lead, if the playback-bypass inference holds.
5. Whether the 16k and 48k images report the same version, and a WS-rate detection routine.
6. Whether GPIO9 is driven by the XU316, by measuring it with the ESP's pin as input only.
7. The ROM-banner noise at the speaker on power-on, on a USJ reset with and without `rtc_suppress_rom_log`, and during flashing.
8. The GDMA prefetch lead (`fetch_lead`) at 32-bit stereo 48 kHz.
9. How long an I2C DFU takes, if the port ships I2C DFU.
