# Loopback spike results, 2026-09-26 17:55

### Heap at connect (HELLO)
- firmware spike-loopback
- free 8-bit heap: boot 264.0 KiB, after WiFi 216.1 KiB, after TCP connect 197.9 KiB (largest block 108.0 KiB, min ever 197.5 KiB)

### stats (manual)
- free 8-bit 198.1 KiB, largest block 108.0 KiB, min ever 194.6 KiB, internal 225.6 KiB, RSSI -52 dBm

### mic: mic TCP 20 ms
- TCP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 3.0 s
- capture->hub (last sample of chunk): min 1.1 / p50 1.5 / p95 4.3 / p99 24.7 / max 54.5 ms (n=151)
-   => worst-case mouth->hub = p99 44.7 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.4 / p95 3.2 / p99 23.6 / max 53.3 ms (n=151)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.7 ms (offset error <= 1.8 ms)
- switch into mic: teardown 0.1 + setup 1.8 ms, first 10 ms DMA block after 19.908 ms
- level: DC -1492 LSB, noise floor -74.9 dBFS, loud (p95 20 ms RMS) -58.7 dBFS, peak -17.4 dBFS; clean.wav got +11.4 dB
- DC settle after enable: 165 ms (valid only if quiet at start)
- heap while streaming: free 195.3 KiB, min ever 191.4 KiB, RSSI -52 dBm
- wav: spike_out/mic-175919-{raw,clean}.wav

### stats (idle)
- free 8-bit 195.3 KiB, largest block 108.0 KiB, min ever 186.1 KiB, internal 222.8 KiB, RSSI -52 dBm

### auto: mic TCP 20 ms
- TCP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 10.0 s
- capture->hub (last sample of chunk): min 1.1 / p50 1.5 / p95 4.9 / p99 8.4 / max 32.4 ms (n=501)
-   => worst-case mouth->hub = p99 28.4 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.4 / p95 3.8 / p99 7.3 / max 31.2 ms (n=501)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.8 ms (offset error <= 1.9 ms)
- switch into mic: teardown 0.0 + setup 0.1 ms, first 10 ms DMA block after 19.92 ms
- level: DC -1599 LSB, noise floor -67.3 dBFS, loud (p95 20 ms RMS) -64.4 dBFS, peak -19.2 dBFS; clean.wav got +13.2 dB
- DC settle after enable: 160 ms (valid only if quiet at start)
- heap while streaming: free 195.3 KiB, min ever 186.1 KiB, RSSI -53 dBm
- wav: spike_out/auto-175941-{raw,clean}.wav

### auto: mic UDP 20 ms
- UDP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 10.0 s
- capture->hub (last sample of chunk): min 1.0 / p50 1.4 / p95 5.1 / p99 8.8 / max 33.4 ms (n=501)
-   => worst-case mouth->hub = p99 28.8 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.4 / p95 4.1 / p99 7.8 / max 32.4 ms (n=501)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.4 ms (offset error <= 1.7 ms)
- switch into mic: teardown 0.0 + setup 0.1 ms, first 10 ms DMA block after 19.92 ms
- level: DC -1625 LSB, noise floor -66.8 dBFS, loud (p95 20 ms RMS) -64.7 dBFS, peak -23.0 dBFS; clean.wav got +17.0 dB
- DC settle after enable: 60 ms (valid only if quiet at start)
- heap while streaming: free 195.3 KiB, min ever 186.1 KiB, RSSI -53 dBm
- wav: spike_out/auto-175951-{raw,clean}.wav

### auto: mic TCP 10 ms
- TCP, 10 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 10.0 s
- capture->hub (last sample of chunk): min 0.9 / p50 1.3 / p95 4.6 / p99 7.7 / max 29.1 ms (n=1002)
-   => worst-case mouth->hub = p99 17.7 ms (+10 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.4 / p95 3.7 / p99 6.8 / max 28.2 ms (n=1002)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.3 ms (offset error <= 1.6 ms)
- switch into mic: teardown 0.0 + setup 0.1 ms, first 10 ms DMA block after 9.921 ms
- level: DC -1628 LSB, noise floor -66.8 dBFS, loud (p95 20 ms RMS) -64.6 dBFS, peak -22.3 dBFS; clean.wav got +16.3 dB
- DC settle after enable: 70 ms (valid only if quiet at start)
- heap while streaming: free 195.3 KiB, min ever 186.1 KiB, RSSI -53 dBm
- wav: spike_out/auto-180002-{raw,clean}.wav

### auto sweep: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 5.0 s
- hub sends first chunk -> first sample to DAC: 6.3 ms
- hub->device one-way per chunk: min 1.5 / p50 2.3 / p95 5.0 / p99 20.0 / max 47.4 ms (n=250)
- downlink jitter (transit - min): min 0.0 / p50 0.8 / p95 3.5 / p99 18.5 / max 45.9 ms (n=250)
- underruns: 0 x 10 ms DMA blocks; played 5.00 s
- switch into speaker: teardown 0.4 + setup 1.4 ms
- clock sync: min RTT 3.1 ms

### switch x10, variant 0 (delete/create)
- speaker->mic: teardown min 0.4 / p50 0.4 / p95 0.4 / p99 0.4 / max 0.4 ms (n=10)
- speaker->mic: setup min 1.2 / p50 1.2 / p95 1.2 / p99 1.2 / max 1.2 ms (n=10)
- speaker->mic: MIC_START received -> mic enabled min 1.7 / p50 1.7 / p95 1.7 / p99 1.7 / max 1.7 ms (n=10)
- mic: enable -> first 10 ms block min 9.9 / p50 9.9 / p95 9.9 / p99 9.9 / max 9.9 ms (n=10)
- mic: DC settle after enable min 50.0 / p50 70.0 / p95 86.0 / p99 93.2 / max 95.0 ms (n=10)
- mic->speaker: teardown min 0.3 / p50 0.4 / p95 0.4 / p99 0.4 / max 0.4 ms (n=10)
- mic->speaker: setup (then preload + enable) min 1.2 / p50 1.3 / p95 1.3 / p99 1.3 / max 1.3 ms (n=10)

### switch x10, variant 1 (keep both, re-route G33)
- speaker->mic: teardown min 0.0 / p50 0.0 / p95 0.0 / p99 0.0 / max 0.0 ms (n=10)
- speaker->mic: setup min 0.1 / p50 0.1 / p95 0.1 / p99 0.1 / max 0.1 ms (n=10)
- speaker->mic: MIC_START received -> mic enabled min 0.1 / p50 0.1 / p95 0.2 / p99 0.2 / max 0.2 ms (n=10)
- mic: enable -> first 10 ms block min 9.9 / p50 9.9 / p95 9.9 / p99 9.9 / max 9.9 ms (n=10)
- mic: DC settle after enable min 65.0 / p50 70.0 / p95 75.0 / p99 75.0 / max 75.0 ms (n=10)
- mic->speaker: teardown min 0.0 / p50 0.0 / p95 0.0 / p99 0.0 / max 0.0 ms (n=10)
- mic->speaker: setup (then preload + enable) min 0.0 / p50 0.0 / p95 0.0 / p99 0.0 / max 0.0 ms (n=10)

### stats (after auto)
- free 8-bit 188.2 KiB, largest block 108.0 KiB, min ever 182.8 KiB, internal 215.7 KiB, RSSI -52 dBm

### ptt: mic TCP 20 ms
- TCP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 2.4 s
- capture->hub (last sample of chunk): min 1.1 / p50 1.4 / p95 3.4 / p99 9.8 / max 10.5 ms (n=121)
-   => worst-case mouth->hub = p99 29.8 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.3 / p95 2.3 / p99 8.7 / max 9.4 ms (n=121)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.4 ms (offset error <= 1.7 ms)
- switch into mic: teardown 0.6 + setup 1.4 ms, first 10 ms DMA block after 19.913 ms
- level: DC -1484 LSB, noise floor -70.8 dBFS, loud (p95 20 ms RMS) -42.6 dBFS, peak -18.3 dBFS; clean.wav got +12.3 dB
- DC settle after enable: 160 ms (valid only if quiet at start)
- wav: spike_out/ptt-180206-{raw,clean}.wav

### ptt-echo: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 2.4 s
- hub sends first chunk -> first sample to DAC: 7.6 ms
- hub->device one-way per chunk: min 1.4 / p50 2.2 / p95 4.7 / p99 8.1 / max 13.1 ms (n=121)
- downlink jitter (transit - min): min 0.0 / p50 0.8 / p95 3.3 / p99 6.7 / max 11.7 ms (n=121)
- underruns: 0 x 10 ms DMA blocks; played 2.42 s
- switch into speaker: teardown 0.4 + setup 1.3 ms
- clock sync: min RTT 3.2 ms

### ptt: mic TCP 20 ms
- TCP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 2.1 s
- capture->hub (last sample of chunk): min 1.4 / p50 1.8 / p95 6.4 / p99 36.5 / max 56.8 ms (n=103)
-   => worst-case mouth->hub = p99 56.5 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.3 / p95 5.0 / p99 35.1 / max 55.4 ms (n=103)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.2 ms (offset error <= 1.6 ms)
- switch into mic: teardown 0.4 + setup 1.3 ms, first 10 ms DMA block after 19.912 ms
- level: DC -1469 LSB, noise floor -66.1 dBFS, loud (p95 20 ms RMS) -39.5 dBFS, peak -19.4 dBFS; clean.wav got +13.4 dB
- DC settle after enable: 185 ms (valid only if quiet at start)
- wav: spike_out/ptt-180216-{raw,clean}.wav

### ptt-echo: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 2.1 s
- hub sends first chunk -> first sample to DAC: 7.2 ms
- hub->device one-way per chunk: min 1.4 / p50 2.1 / p95 6.0 / p99 11.3 / max 11.8 ms (n=103)
- downlink jitter (transit - min): min 0.0 / p50 0.7 / p95 4.6 / p99 9.9 / max 10.4 ms (n=103)
- underruns: 0 x 10 ms DMA blocks; played 2.06 s
- switch into speaker: teardown 0.3 + setup 1.3 ms
- clock sync: min RTT 3.2 ms

### ptt: mic TCP 20 ms
- TCP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 2.0 s
- capture->hub (last sample of chunk): min 1.4 / p50 1.8 / p95 5.2 / p99 19.4 / max 39.4 ms (n=101)
-   => worst-case mouth->hub = p99 39.4 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.3 / p95 3.8 / p99 18.0 / max 37.9 ms (n=101)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.2 ms (offset error <= 1.6 ms)
- switch into mic: teardown 0.4 + setup 1.2 ms, first 10 ms DMA block after 19.912 ms
- level: DC -1503 LSB, noise floor -66.7 dBFS, loud (p95 20 ms RMS) -41.9 dBFS, peak -21.8 dBFS; clean.wav got +15.8 dB
- DC settle after enable: 130 ms (valid only if quiet at start)
- wav: spike_out/ptt-180221-{raw,clean}.wav

### ptt-echo: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 2.0 s
- hub sends first chunk -> first sample to DAC: 9.2 ms
- hub->device one-way per chunk: min 1.7 / p50 2.5 / p95 6.4 / p99 11.5 / max 13.4 ms (n=101)
- downlink jitter (transit - min): min 0.0 / p50 0.8 / p95 4.7 / p99 9.8 / max 11.7 ms (n=101)
- underruns: 0 x 10 ms DMA blocks; played 2.02 s
- switch into speaker: teardown 0.3 + setup 1.3 ms
- clock sync: min RTT 3.2 ms

### ptt: mic TCP 20 ms
- TCP, 20 ms chunks, variant 0, slot R, PDM clk 2.048 MHz, 1.6 s
- capture->hub (last sample of chunk): min 1.4 / p50 1.6 / p95 3.6 / p99 7.8 / max 18.3 ms (n=78)
-   => worst-case mouth->hub = p99 27.8 ms (+20 ms chunk fill)
- arrival jitter (transit - min): min 0.0 / p50 0.3 / p95 2.3 / p99 6.4 / max 16.9 ms (n=78)
- lost 0, reordered 0, device send failures 0, mic DMA overruns 0
- clock sync: min RTT 3.2 ms (offset error <= 1.6 ms)
- switch into mic: teardown 0.4 + setup 1.3 ms, first 10 ms DMA block after 19.913 ms
- level: DC -1428 LSB, noise floor -70.0 dBFS, loud (p95 20 ms RMS) -40.3 dBFS, peak -20.6 dBFS; clean.wav got +14.6 dB
- DC settle after enable: 145 ms (valid only if quiet at start)
- wav: spike_out/ptt-180228-{raw,clean}.wav

### ptt-echo: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 1.6 s
- hub sends first chunk -> first sample to DAC: 7.6 ms
- hub->device one-way per chunk: min 1.5 / p50 2.3 / p95 6.1 / p99 10.4 / max 11.9 ms (n=78)
- downlink jitter (transit - min): min 0.0 / p50 0.8 / p95 4.6 / p99 8.8 / max 10.4 ms (n=78)
- underruns: 0 x 10 ms DMA blocks; played 1.56 s
- switch into speaker: teardown 0.3 + setup 1.3 ms
- clock sync: min RTT 3.2 ms

### play sweep: speaker
- mono, prefill 40 ms, hub lead 60 ms, variant 0, 5.0 s
- hub sends first chunk -> first sample to DAC: 8.7 ms
- hub->device one-way per chunk: min 1.3 / p50 2.0 / p95 5.4 / p99 11.2 / max 13.2 ms (n=250)
- downlink jitter (transit - min): min 0.0 / p50 0.8 / p95 4.1 / p99 9.9 / max 11.9 ms (n=250)
- underruns: 0 x 10 ms DMA blocks; played 5.00 s
- switch into speaker: teardown 0.4 + setup 1.3 ms
- clock sync: min RTT 3.0 ms

### play sweep: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 5.0 s
- hub sends first chunk -> first sample to DAC: 6.9 ms
- hub->device one-way per chunk: min 1.3 / p50 2.0 / p95 5.2 / p99 8.1 / max 11.0 ms (n=250)
- downlink jitter (transit - min): min 0.0 / p50 0.7 / p95 3.9 / p99 6.9 / max 9.8 ms (n=250)
- underruns: 0 x 10 ms DMA blocks; played 5.00 s
- switch into speaker: teardown 0.4 + setup 1.3 ms
- clock sync: min RTT 3.0 ms

### play tone: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 20.0 s
- hub sends first chunk -> first sample to DAC: 7.2 ms
- hub->device one-way per chunk: min 1.5 / p50 2.3 / p95 5.4 / p99 8.1 / max 10.7 ms (n=466)
- downlink jitter (transit - min): min 0.0 / p50 0.9 / p95 3.9 / p99 6.7 / max 9.2 ms (n=466)
- underruns: 0 x 10 ms DMA blocks; played 9.32 s
- switch into speaker: teardown 0.0 + setup 0.0 ms
- clock sync: min RTT 3.1 ms
- BARGE-IN: button edge -> STOP at device 11.4 ms, -> speaker silent 16.8 ms

### play tone: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 3.0 s
- hub sends first chunk -> first sample to DAC: 7.2 ms
- hub->device one-way per chunk: min 1.5 / p50 2.3 / p95 5.6 / p99 8.3 / max 10.0 ms (n=150)
- downlink jitter (transit - min): min 0.0 / p50 0.8 / p95 4.1 / p99 6.8 / max 8.5 ms (n=150)
- underruns: 0 x 10 ms DMA blocks; played 3.00 s
- switch into speaker: teardown 0.0 + setup 0.0 ms
- clock sync: min RTT 3.2 ms

### play tone: speaker
- stereo L=R, prefill 40 ms, hub lead 60 ms, variant 0, 3.0 s
- hub sends first chunk -> first sample to DAC: 7.2 ms
- hub->device one-way per chunk: min 1.6 / p50 2.5 / p95 5.2 / p99 8.0 / max 11.5 ms (n=150)
- downlink jitter (transit - min): min 0.0 / p50 0.9 / p95 3.6 / p99 6.5 / max 9.9 ms (n=150)
- underruns: 0 x 10 ms DMA blocks; played 3.00 s
- switch into speaker: teardown 0.0 + setup 0.0 ms
- clock sync: min RTT 3.1 ms

### Human ear tests and follow-up analysis (2026-09-26)
- PTT voice echo (hold button, speak, hear it back on the Echo): "works pretty well".
- Sweep mono vs stereo L=R: no strong difference heard; chose stereo L=R (ESPHome reports mono as tinny).
- 330 Hz tone at -12 dBFS: inaudible, only a click at onset. 1 kHz: clearly audible. The speaker's low end sits between 330 Hz and 1 kHz.
- Click audible when playback starts (likely NS4168 turn-on when BCLK starts).
- Barge-in (button during the 20 s tone): edge -> STOP at device 11.4 ms, -> speaker silent 16.8 ms. ~5 ms of that is the audio task finishing a blocked 10 ms I2S write.
- Mic start transient (raw WAVs, DC per 5 ms window): ~15 ms of flat constant output after enable (mic waking), then the DC offset (~-1500 LSB) settles exponentially over ~50-70 ms. AC content is usable after ~20 ms once the hub high-passes.
- Setup: PC hub on Ethernet; Echo on 2.4 GHz WiFi, RSSI about -52 dBm, WIFI_PS_NONE, default ESP-IDF 5.5 sdkconfig.
