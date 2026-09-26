#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy"]
# ///
"""PROTOTYPE, throwaway: hub side of the Atom Echo audio loopback spike (issue #7). Not the hub.

    uv run hub/spike_loopback.py

Then power the Echo (firmware/spike-loopback); it connects here on TCP 7000. Type `help`.
WAVs and results.md land in hub/spike_out/.
"""

import asyncio
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

PORT, UDP_PORT, RATE = 7000, 7001, 16000
OUT = Path(__file__).parent / "spike_out"
SESSION = datetime.now().strftime("%Y-%m-%d %H:%M")

# Frame types, mirrored from firmware/spike-loopback/src/main.c
PING, MIC_START, MIC_STOP, SPK_START, SPK_DATA, SPK_END, SPK_STOP, STATS_REQ = range(0x01, 0x09)
HELLO, PONG, MIC_DATA, MIC_STARTED, MIC_DONE, BTN, SPK_READY, SPK_PLAYING, SPK_ACK, SPK_DONE, STATS = range(0x81, 0x8C)

HELP = """\
  sync                         clock offset + RTT to the device
  stats                        device heap + RSSI
  probe                        record 1.5 s on each PDM slot (make noise) and pick the live one
  mic [secs=5] [chunk=20] [udp] [v=0] [slot=0|1] [dsr=16|8]
                               stream the mic; latency/jitter/level report, WAVs saved
  ptt [on|off]                 hold the button to talk; on release the recording plays back on the Echo
  play <tone|sweep|last|silence|file> [mono] [gain=0] [prefill=40] [lead=60] [v=0] [secs=3] [f=440]
                               stream to the speaker; press the button mid-play to measure barge-in
  switch [n=10] [v=0]          flip mic<->speaker n times (stay quiet), switch-time report
  auto                         stats, mic tcp/udp, sweep, switch v0+v1: the numbers pass
  listen [raw|clean]           play the last mic recording on this PC
  summary                      print results.md so far
  quit
  (v = I2S switching variant: 0 = delete/create channels, 1 = keep both, re-route G33)"""


def now_us():
    return time.monotonic_ns() // 1000


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


REPORT: list[str] = []


def report(title, lines):
    REPORT.extend([f"### {title}", *(f"- {line}" for line in lines), ""])
    OUT.mkdir(exist_ok=True)
    (OUT / "results.md").write_text(f"# Loopback spike results, {SESSION}\n\n" + "\n".join(REPORT))
    log(title)
    for line in lines:
        print("    " + line, flush=True)


def dist(a, unit="ms"):
    a = np.asarray(a, float)
    if not len(a):
        return "n/a"
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return f"min {a.min():.1f} / p50 {p50:.1f} / p95 {p95:.1f} / p99 {p99:.1f} / max {a.max():.1f} {unit} (n={len(a)})"


def dbfs(v):
    return 20 * np.log10(max(float(v), 1e-9) / 32768)


def save_wav(path, pcm):
    OUT.mkdir(exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype("<i2").tobytes())



# ------------------------------------------------------------------ signals


def tone(secs=3.0, f=440.0, db=-12.0):
    t = np.arange(int(secs * RATE)) / RATE
    return fade(np.sin(2 * np.pi * f * t) * 10 ** (db / 20) * 32767)


def sweep(secs=5.0, f0=100.0, f1=7000.0, db=-12.0):
    t = np.arange(int(secs * RATE)) / RATE
    k = np.log(f1 / f0)
    return fade(np.sin(2 * np.pi * f0 * secs / k * (np.exp(t * k / secs) - 1)) * 10 ** (db / 20) * 32767)


def fade(x, ms=10):
    n = RATE * ms // 1000
    ramp = np.linspace(0, 1, n)
    x[:n] *= ramp
    x[-n:] *= ramp[::-1]
    return x.astype(np.int16)


def load_audio(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, "<i2").copy()


def highpass(x):
    # crude DC/rumble removal: subtract a 50 ms moving average
    k = np.ones(801) / 801
    return x - np.convolve(x, k, mode="same")


def settle_ms(pcm):
    """When the mic's DC level stops moving after it's switched on. Only meaningful in a quiet room."""
    x = pcm[: RATE * 400 // 1000].astype(float)
    if len(x) < RATE * 400 // 1000:
        return None
    w = x.reshape(-1, 80).mean(axis=1)  # DC per 5 ms
    steady = w[len(w) // 2 :]
    bad = np.nonzero(np.abs(w - np.median(steady)) > 4 * steady.std() + 30)[0]
    return 0 if not len(bad) else int(bad.max() + 1) * 5


# ------------------------------------------------------------------ device


class Dev:
    def __init__(self):
        self.w = None
        self.sync = deque(maxlen=75)  # (t_pc, rtt_us, offset_us); offset = device clock - PC clock
        self.waiters = {}
        self.mic = None  # [(seq, t_cap_end_dev, t_recv_pc, pcm_bytes)] while recording
        self.acks = None  # {seq: t_dev_recv} while playing
        self.playing = None  # dict while a play session runs; "stop" = button edge that aborted it
        self.ptt = False
        self.ptt_task = None
        self.ptt_stop = None
        self.slot = 1
        self.last_raw = self.last_clean = None
        self.ping_seq = 0

    @property
    def up(self):
        return self.w is not None and not self.w.is_closing()

    def send(self, t, payload=b""):
        self.w.write(struct.pack("<BBH", t, 0, len(payload)) + payload)

    def expect(self, t):
        f = asyncio.get_running_loop().create_future()
        self.waiters[t] = f
        return f

    async def call(self, t, payload, reply, timeout=3):
        f = self.expect(reply)
        self.send(t, payload)
        return await asyncio.wait_for(f, timeout)

    def offset(self):
        """(offset_us, rtt_us) of the lowest-RTT ping in the last 15 s."""
        cutoff = now_us() - 15_000_000
        recent = [s for s in self.sync if s[0] > cutoff] or list(self.sync)
        best = min(recent, key=lambda s: s[1])
        return best[2], best[1]

    def dev2pc(self, t_dev):
        return t_dev - self.offset()[0]

    async def stats(self):
        p, _ = await self.call(STATS_REQ, b"", STATS)
        return dict(zip(("free8", "largest8", "minfree8", "free_internal", "rssi"), struct.unpack("<IIIIi", p)))

    def dispatch(self, t, p, t_recv):
        if t == PONG:
            _, t_pc, t_dev = struct.unpack("<Iqq", p)
            self.sync.append((t_recv, t_recv - t_pc, t_dev - (t_pc + t_recv) // 2))
        elif t == MIC_DATA:
            if self.mic is not None:
                seq, t_cap = struct.unpack_from("<Iq", p)
                self.mic.append((seq, t_cap, t_recv, p[12:]))
        elif t == SPK_ACK:
            if self.acks is not None:
                seq, t_dev = struct.unpack("<Iq", p)
                self.acks[seq] = t_dev
        elif t == BTN:
            self.on_button(*struct.unpack("<Bqq", p), t_recv)
        elif t == HELLO:
            boot, wifi, conn, largest, minfree, fw = struct.unpack("<IIIII16s", p)
            report("Heap at connect (HELLO)", [
                f"firmware {fw.rstrip(b'\0').decode()}",
                f"free 8-bit heap: boot {boot/1024:.1f} KiB, after WiFi {wifi/1024:.1f} KiB, "
                f"after TCP connect {conn/1024:.1f} KiB (largest block {largest/1024:.1f} KiB, min ever {minfree/1024:.1f} KiB)",
            ])
        else:
            f = self.waiters.pop(t, None)
            if f and not f.done():
                f.set_result((p, t_recv))

    def on_button(self, down, t_edge, t_sent, t_recv):
        if self.sync:
            lat = (t_recv - self.dev2pc(t_edge)) / 1000
            log(f"button {'down' if down else 'up'}: edge->hub {lat:.1f} ms (incl. {(t_sent - t_edge) / 1000:.1f} ms debounce)")
            BUTTON_LAT.append(lat)
        if down and self.playing is not None and "stop" not in self.playing:
            self.send(SPK_STOP)  # barge-in: stop the speaker right away
            self.playing["stop"] = t_edge
        elif self.ptt and down and self.ptt_task is None:
            self.ptt_stop = asyncio.Event()
            self.ptt_task = asyncio.create_task(ptt_session(self, t_edge))
        elif self.ptt and not down and self.ptt_stop:
            self.ptt_stop.set()


BUTTON_LAT: list[float] = []
dev = Dev()


# ------------------------------------------------------------------ sessions


async def mic_session(d, secs=None, chunk=20, udp=False, v=0, slot=None, dsr=16, stop_evt=None, label="mic", quiet=False):
    slot = d.slot if slot is None else slot
    d.mic = []
    p, _ = await d.call(MIC_START, struct.pack("<BBBBBH", int(udp), slot, v, chunk, int(dsr == 16), UDP_PORT), MIC_STARTED)
    t_cmd, t_en, teardown, setup = struct.unpack("<qqII", p)
    st = None
    if stop_evt is not None:
        await stop_evt.wait()
    elif secs >= 2 and not quiet:
        await asyncio.sleep(secs / 2)
        st = await d.stats()
        await asyncio.sleep(secs / 2)
    else:
        await asyncio.sleep(secs)
    p, _ = await d.call(MIC_STOP, b"", MIC_DONE)
    chunks, send_fail, dropped = struct.unpack("<III", p)
    await asyncio.sleep(0.05)  # UDP stragglers
    frames, d.mic = d.mic, None
    if not frames:
        log("no mic frames arrived")
        return None

    seqs = [f[0] for f in frames]
    run_max, reordered = -1, 0
    for s in seqs:
        reordered += s < run_max
        run_max = max(run_max, s)
    lost = run_max + 1 - len(set(seqs))
    frames.sort(key=lambda f: f[0])
    off, rtt = d.offset()
    lat = np.array([(f[2] - (f[1] - off)) / 1000 for f in frames])  # last sample of chunk captured -> at hub
    transit = np.array([f[2] - f[1] for f in frames]) / 1000  # offset-free
    jitter = transit - transit.min()
    n = chunk * RATE // 1000
    pcm = np.zeros((run_max + 1) * n, np.int16)
    for s, _, _, b in frames:
        a = np.frombuffer(b, "<i2")
        pcm[s * n : s * n + len(a)] = a
    first_ms = (frames[0][1] - t_en) / 1000 if frames[0][0] == 0 else None

    x = pcm.astype(float)
    y = highpass(x)
    wins = y[: len(y) // 320 * 320].reshape(-1, 320)
    wr = np.sqrt((wins**2).mean(axis=1)) if len(wins) else np.zeros(1)
    peak = np.abs(y).max() if len(y) else 0
    gain = min(10 ** (30 / 20), 0.5 * 32767 / max(peak, 1))
    clean = np.clip(y * gain, -32767, 32767).astype(np.int16)
    r = dict(t_cmd=t_cmd, t_en=t_en, teardown=teardown, setup=setup, first_ms=first_ms, lat=lat, jitter=jitter,
             std=float(y.std()), dc=float(x.mean()), settle=settle_ms(pcm), pcm=pcm)
    if quiet:
        return r

    d.last_raw, d.last_clean = pcm, clean
    stamp = datetime.now().strftime("%H%M%S")
    save_wav(OUT / f"{label}-{stamp}-raw.wav", pcm)
    save_wav(OUT / f"{label}-{stamp}-clean.wav", clean)
    lines = [
        f"{'UDP' if udp else 'TCP'}, {chunk} ms chunks, variant {v}, slot {'R' if slot else 'L'}, PDM clk {'2.048' if dsr == 16 else '1.024'} MHz, {len(pcm) / RATE:.1f} s",
        f"capture->hub (last sample of chunk): {dist(lat)}",
        f"  => worst-case mouth->hub = p99 {np.percentile(lat, 99) + chunk:.1f} ms (+{chunk} ms chunk fill)",
        f"arrival jitter (transit - min): {dist(jitter)}",
        f"lost {lost}, reordered {reordered}, device send failures {send_fail}, mic DMA overruns {dropped}",
        f"clock sync: min RTT {rtt / 1000:.1f} ms (offset error <= {rtt / 2000:.1f} ms)",
        f"switch into mic: teardown {teardown / 1000:.1f} + setup {setup / 1000:.1f} ms, first 10 ms DMA block after {first_ms} ms",
        f"level: DC {x.mean():.0f} LSB, noise floor {dbfs(np.percentile(wr, 10)):.1f} dBFS, "
        f"loud (p95 20 ms RMS) {dbfs(np.percentile(wr, 95)):.1f} dBFS, peak {dbfs(peak):.1f} dBFS; clean.wav got +{20 * np.log10(gain):.1f} dB",
        f"DC settle after enable: {r['settle']} ms (valid only if quiet at start)",
    ]
    if y.std() < 2:
        lines.append("WARNING: mic data is flat; wrong PDM slot? try `probe` or slot=0/1")
    if st:
        lines.append(f"heap while streaming: free {st['free8'] / 1024:.1f} KiB, min ever {st['minfree8'] / 1024:.1f} KiB, RSSI {st['rssi']} dBm")
    lines.append(f"wav: spike_out/{label}-{stamp}-{{raw,clean}}.wav")
    report(f"{label}: mic {'UDP' if udp else 'TCP'} {chunk} ms", lines)
    return r


async def play_session(d, pcm, mono=False, prefill=40, lead=60, v=0, label="play", quiet=False):
    chunk = 320  # 20 ms
    n = (len(pcm) + chunk - 1) // chunk
    d.acks, d.playing = {}, {}
    playing_f, done_f = d.expect(SPK_PLAYING), d.expect(SPK_DONE)
    p, _ = await d.call(SPK_START, struct.pack("<BBH", int(not mono), v, prefill), SPK_READY)
    _, teardown, setup = struct.unpack("<qII", p)
    t0, sends = now_us(), []
    for i in range(n):
        wait = t0 + i * 20_000 - lead * 1000 - now_us()  # real-time pacing, `lead` ms ahead
        if wait > 0:
            await asyncio.sleep(wait / 1e6)
        if "stop" in d.playing:
            break
        ts = now_us()
        d.send(SPK_DATA, struct.pack("<Iq", i, ts) + pcm[i * chunk : (i + 1) * chunk].astype("<i2").tobytes())
        sends.append(ts)
    if "stop" not in d.playing:
        d.send(SPK_END)
    p, _ = await asyncio.wait_for(done_f, len(pcm) / RATE + 5)
    aborted, t_cmd, t_stop, underruns, played = struct.unpack("<BqqII", p)
    await asyncio.sleep(0.05)
    acks, stop_edge = d.acks, d.playing.get("stop")
    d.acks = d.playing = None
    off, rtt = d.offset()
    r = dict(teardown=teardown, setup=setup, start_ms=None)
    if playing_f.done():
        t_enable, preloaded = struct.unpack("<qI", playing_f.result()[0])
        r["start_ms"] = (t_enable - off - sends[0]) / 1000
    else:
        playing_f.cancel()
    if quiet:
        return r

    oneway = [(acks[i] - off - s) / 1000 for i, s in enumerate(sends) if i in acks]
    transit = np.array([(acks[i] - s) / 1000 for i, s in enumerate(sends) if i in acks])
    lines = [
        f"{'mono' if mono else 'stereo L=R'}, prefill {prefill} ms, hub lead {lead} ms, variant {v}, {len(pcm) / RATE:.1f} s",
        f"hub sends first chunk -> first sample to DAC: {r['start_ms']:.1f} ms" if r["start_ms"] is not None else "never started",
        f"hub->device one-way per chunk: {dist(oneway)}",
        f"downlink jitter (transit - min): {dist(transit - transit.min() if len(transit) else [])}",
        f"underruns: {underruns} x 10 ms DMA blocks; played {played / (4 if not mono else 2) / RATE:.2f} s",
        f"switch into speaker: teardown {teardown / 1000:.1f} + setup {setup / 1000:.1f} ms",
        f"clock sync: min RTT {rtt / 1000:.1f} ms",
    ]
    if aborted and stop_edge:
        lines.append(f"BARGE-IN: button edge -> STOP at device {(t_cmd - stop_edge) / 1000:.1f} ms, "
                     f"-> speaker silent {(t_stop - stop_edge) / 1000:.1f} ms")
    report(f"{label}: speaker", lines)
    return r


async def ptt_session(d, t_edge):
    try:
        r = await mic_session(d, stop_evt=d.ptt_stop, label="ptt")
        if r:
            log(f"button down -> mic enabled {(r['t_en'] - t_edge) / 1000:.1f} ms (device clock); echoing back")
            await play_session(d, d.last_clean, label="ptt-echo")
    finally:
        d.ptt_task = d.ptt_stop = None


async def switch_test(d, n=10, v=0):
    log(f"switch test, variant {v}: stay quiet for ~{n * 0.6:.0f} s")
    sil = np.zeros(RATE // 10, np.int16)
    rows = []
    for i in range(n + 1):  # first round warms up the variant's channel layout; dropped
        m = await mic_session(d, secs=0.4, chunk=10, v=v, quiet=True)
        s = await play_session(d, sil, prefill=10, lead=0, v=v, quiet=True)
        if i and m and s:
            rows.append((m["teardown"] / 1000, m["setup"] / 1000, (m["t_en"] - m["t_cmd"]) / 1000,
                         m["first_ms"] or 0, m["settle"] or 0, s["teardown"] / 1000, s["setup"] / 1000))
    a = np.array(rows)
    report(f"switch x{n}, variant {v} ({'delete/create' if v == 0 else 'keep both, re-route G33'})", [
        f"speaker->mic: teardown {dist(a[:, 0])}",
        f"speaker->mic: setup {dist(a[:, 1])}",
        f"speaker->mic: MIC_START received -> mic enabled {dist(a[:, 2])}",
        f"mic: enable -> first 10 ms block {dist(a[:, 3])}",
        f"mic: DC settle after enable {dist(a[:, 4])}",
        f"mic->speaker: teardown {dist(a[:, 5])}",
        f"mic->speaker: setup (then preload + enable) {dist(a[:, 6])}",
    ])


async def probe(d):
    log("probe: make noise (talk, tap the case) for 3 s")
    res = {}
    for slot in (0, 1):
        r = await mic_session(d, secs=1.5, slot=slot, quiet=True)
        res[slot] = r["std"] if r else 0
        log(f"slot {'LR'[slot]}: std {res[slot]:.1f} LSB, DC {r['dc'] if r else 0:.0f}")
    d.slot = max(res, key=res.get)
    log(f"using slot {'LR'[d.slot]}; `listen` to check it's really speech")


async def show_stats(d, label):
    s = await d.stats()
    report(f"stats ({label})", [f"free 8-bit {s['free8'] / 1024:.1f} KiB, largest block {s['largest8'] / 1024:.1f} KiB, "
                                f"min ever {s['minfree8'] / 1024:.1f} KiB, internal {s['free_internal'] / 1024:.1f} KiB, RSSI {s['rssi']} dBm"])


async def auto(d):
    await show_stats(d, "idle")
    log("auto: mic tests, talk or stay quiet, both are useful")
    await mic_session(d, 10, 20, udp=False, label="auto")
    await mic_session(d, 10, 20, udp=True, label="auto")
    await mic_session(d, 10, 10, udp=False, label="auto")
    await play_session(d, sweep(), label="auto sweep")
    await switch_test(d, 10, 0)
    await switch_test(d, 10, 1)
    await show_stats(d, "after auto")
    if BUTTON_LAT:
        report("button edge->hub", [dist(BUTTON_LAT)])


# ------------------------------------------------------------------ plumbing


def opts(args, **defaults):
    o = dict(defaults)
    for a in args:
        k, _, v = a.partition("=")
        o[k] = type(defaults[k])(v) if v and k in defaults else (v or True)
    return o


async def run_cmd(d, args):
    cmd, rest = args[0], args[1:]
    if cmd == "help":
        print(HELP)
    elif cmd == "quit":
        raise SystemExit
    elif cmd == "summary":
        print("\n".join(REPORT) or "(nothing yet)")
    elif cmd == "listen":
        pcm = d.last_raw if rest[:1] == ["raw"] else d.last_clean
        if pcm is None:
            log("nothing recorded yet")
            return
        save_wav(OUT / "listen.wav", pcm)
        player = shutil.which("pw-play") or shutil.which("aplay")
        await asyncio.to_thread(subprocess.run, [player, str(OUT / "listen.wav")])
    elif not d.up or not d.sync:
        log("device not connected (or no clock sync yet)")
    elif cmd == "sync":
        off, rtt = d.offset()
        rtts = sorted(s[1] / 1000 for s in d.sync)
        log(f"offset {off / 1000:.1f} ms, RTT min {rtts[0]:.1f} / median {rtts[len(rtts) // 2]:.1f} / max {rtts[-1]:.1f} ms")
    elif cmd == "stats":
        await show_stats(d, "manual")
    elif cmd == "probe":
        await probe(d)
    elif cmd == "mic":
        o = opts(rest, secs=5.0, chunk=20, v=0, slot=d.slot, dsr=16, udp=False)
        await mic_session(d, o["secs"], o["chunk"], bool(o["udp"]), o["v"], o["slot"], o["dsr"])
    elif cmd == "ptt":
        d.ptt = rest[:1] != ["off"]
        log(f"push-to-talk {'on: hold the button and talk' if d.ptt else 'off'}")
    elif cmd == "play":
        src = rest[0] if rest else "tone"
        o = opts(rest[1:], mono=False, gain=0.0, prefill=40, lead=60, v=0, secs=3.0, f=440.0)
        if src == "tone":
            pcm = tone(o["secs"], o["f"])
        elif src == "sweep":
            pcm = sweep(o["secs"] if "secs=" in " ".join(rest) else 5.0)
        elif src == "silence":
            pcm = np.zeros(int(o["secs"] * RATE), np.int16)
        elif src == "last":
            pcm = d.last_clean
        else:
            pcm = load_audio(src)
        if pcm is None:
            log("nothing recorded yet")
            return
        pcm = np.clip(pcm.astype(float) * 10 ** (o["gain"] / 20), -29000, 29000).astype(np.int16)
        await play_session(d, pcm, bool(o["mono"]), o["prefill"], o["lead"], o["v"], label=f"play {src}")
    elif cmd == "switch":
        o = opts(rest, n=10, v=0)
        await switch_test(d, o["n"], o["v"])
    elif cmd == "auto":
        await auto(d)
    else:
        log(f"unknown command {cmd!r}; try help")


async def handle(reader, writer):
    dev.w = writer
    dev.sync.clear()
    writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    log(f"device connected from {writer.get_extra_info('peername')[0]}")
    try:
        while True:
            t, _, n = struct.unpack("<BBH", await reader.readexactly(4))
            p = await reader.readexactly(n) if n else b""
            dev.dispatch(t, p, now_us())
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    log("device disconnected")
    dev.w = None


class Udp(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        t = now_us()
        typ, _, n = struct.unpack_from("<BBH", data)
        dev.dispatch(typ, data[4 : 4 + n], t)


async def pinger():
    while True:
        await asyncio.sleep(0.2)
        if dev.up:
            dev.ping_seq += 1
            dev.send(PING, struct.pack("<Iq", dev.ping_seq, now_us()))


async def main():
    loop = asyncio.get_running_loop()
    await asyncio.start_server(handle, "0.0.0.0", PORT)
    await loop.create_datagram_endpoint(Udp, local_addr=("0.0.0.0", UDP_PORT))
    asyncio.create_task(pinger())
    log(f"listening on TCP {PORT} / UDP {UDP_PORT}; waiting for the Echo. Type `help`.")
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        if args := line.split():
            try:
                await run_cmd(dev, args)
            except SystemExit:
                return
            except Exception as e:  # noqa: BLE001  keep the REPL alive
                log(f"error: {e!r}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
