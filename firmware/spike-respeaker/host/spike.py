"""PROTOTYPE, throwaway: the host side of the ReSpeaker Lite full-duplex I2S spike (#59). Not the
product.

Drives the spike firmware's console (src/main.c) with the acoustic rig: the POD, a USB mic by the
ReSpeaker's mics, and the mouth, the PC speaker aimed at them. Every experiment prints its
numbers and writes WAVs to firmware/spike-respeaker/out/. Run it with the repo's venv:

    .venv/bin/python firmware/spike-respeaker/host/spike.py con info rate
    .venv/bin/python firmware/spike-respeaker/host/spike.py calib | mic | aec | hf | vol |
        cutair | boot | romlog | load

The mic latency needs no host clock: the POD hears the mouth's chirps and the ESP's chirps on one
ADC clock, the XU316's mic output and the ESP's TX share the I2S clock, and loop mode ties TX
frames to RX frames. What's left is the speaker's own small delay (DAC filter + air), stated
separately.
"""

import argparse
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import serial
import soundfile as sf
import soxr

from tinyaisense_hil.acoustic import chirp, click_db, tone_level
from tinyaisense_hil.pod import Mouth, Pod

R = 48000
F = 120  # frames per DMA descriptor
OUT = Path(__file__).resolve().parent.parent / "out"
AUDIO = Path(__file__).resolve().parents[3] / "packages/hub/tests/audio"
POD = "Wavo POD: USB Audio"
MOUTH = "USB Audio Device"
UP = chirp(R)
DOWN = UP[::-1].copy()


def db(x: float) -> float:
    return 20 * np.log10(max(x, 1e-12))


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0


class Dev:
    def __init__(self, port: str) -> None:
        self.s = serial.Serial()
        self.s.port = port
        self.s.baudrate = 115200
        self.s.timeout = 0.5
        # The USB-Serial/JTAG resets the chip on RTS=1, DTR=0: open without passing through it.
        self.s.dtr = True
        self.s.rts = True
        self.s.open()
        time.sleep(0.1)
        self.s.reset_input_buffer()

    def _line(self, deadline: float) -> str:
        while True:
            b = self.s.readline()
            if b:
                return b.decode(errors="replace").rstrip("\r\n")
            if time.time() > deadline:
                raise TimeoutError("no answer from the device")

    def _exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            d = self.s.read(min(65536, n - len(buf)))
            buf += d
            if not d and time.time() > deadline:
                raise TimeoutError(f"dump short: {len(buf)}/{n}")
        return bytes(buf)

    def cmd(self, line: str, timeout: float = 30.0, echo: bool = True) -> list[str]:
        self.s.write((line + "\n").encode())
        return self.done(timeout, echo)

    def done(self, timeout: float = 30.0, echo: bool = True) -> list[str]:
        out: list[str] = []
        deadline = time.time() + timeout
        while True:
            ln = self._line(deadline)
            if ln == "#done":
                return out
            if echo and ln:
                print("   ", ln)
            out.append(ln)

    def load(self, pcm: np.ndarray) -> None:
        data = np.clip(pcm, -32768, 32767).astype("<i2").tobytes()
        self.s.write(f"load {len(data)}\n".encode())
        deadline = time.time() + 10
        while self._line(deadline) != "READY":
            pass
        self.s.write(data)
        lines = self.done(timeout=60, echo=False)
        ok = [ln for ln in lines if ln.startswith("LOADED")]
        assert ok and int(ok[0].split("sum=")[1]) == sum(data) % 2**32, lines

    def dump(self) -> tuple[dict, dict[str, bytes]]:
        self.s.write(b"dump\n")
        info: dict = {}
        bins: dict[str, bytes] = {}
        deadline = time.time() + 120
        while True:
            ln = self._line(deadline)
            if ln.startswith("RUNINFO"):
                info = kv(ln)
            elif ln.startswith("DUMP "):
                _, name, n = ln.split()
                bins[name] = self._exact(int(n), deadline)
            elif ln == "#done":
                return info, bins


def kv(line: str) -> dict:
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


class Run:
    """A dumped run: the capture (ch0, ch1 as floats, full scale 1), and the DMA stamps."""

    def __init__(self, info: dict, bins: dict[str, bytes]) -> None:
        self.info = info
        cap = np.frombuffer(bins["cap"], "<i4").reshape(-1, 2)
        self.raw = cap
        self.ch0 = cap[:, 0] / 2**31
        self.ch1 = cap[:, 1] / 2**31
        now = info["now_us"]
        low = now % 2**32

        def full(stamps: np.ndarray) -> np.ndarray:
            return now - ((low - stamps.astype(np.int64)) % 2**32)

        self.rxt = full(np.frombuffer(bins["rxt"], "<u4"))
        self.txt = full(np.frombuffer(bins["txt"], "<u4"))
        self.frame0 = info["frame0"]
        self.tx_start = info["tx_start"]

    def line_us(self, rx_frame: int) -> float:
        """When the RX frame `rx_frame` (global) was on the line, by its descriptor's stamp."""
        q = rx_frame // F - self.info["rx_base"]
        return float(self.rxt[q]) - (F - 1 - rx_frame % F) * 1e6 / R

    def sent_us(self, seq: int) -> float:
        return float(self.txt[seq - self.info["tx_base"]])

    def check(self) -> None:
        if self.info["tx_qovf"] or self.info["rx_qovf"]:
            print(f"!! DMA queue overflows: tx {self.info['tx_qovf']} rx {self.info['rx_qovf']}")


def run(dev: Dev, ms: int, play: bool, during=None) -> Run:
    dev.cmd(f"run {ms}{' play' if play else ''}", echo=False)
    if during:
        during()
    dev.cmd("wait", timeout=ms / 1000 + 20, echo=False)
    r = Run(*dev.dump())
    r.check()
    return r


def find_all(sig: np.ndarray, tpl: np.ndarray, gap: int, thresh: float = 0.4) -> list[tuple[int, float]]:
    """Every place `tpl` matches `sig` by normalized cross-correlation, at least `gap` apart."""
    n = len(tpl)
    size = 1 << (len(sig) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(sig, size) * np.conj(np.fft.rfft(tpl, size)), size)[: len(sig) - n + 1]
    e = np.cumsum(np.concatenate(([0.0], sig.astype(np.float64) ** 2)))
    match = corr / (np.sqrt(np.maximum(e[n:] - e[:-n], 1e-30)) * np.linalg.norm(tpl))
    peaks: list[tuple[int, float]] = []
    for i in np.argsort(match)[::-1]:
        if match[i] < thresh:
            break
        if all(abs(int(i) - p) >= gap for p, _ in peaks):
            peaks.append((int(i), float(match[i])))
    return sorted(peaks)


def speech(name: str, gain: float = 1.0) -> np.ndarray:
    x, rate = sf.read(AUDIO / name, dtype="float32")
    x = soxr.resample(x, rate, R)
    return x / np.max(np.abs(x)) * gain


def place(total_s: float, items: list[tuple[float, np.ndarray]]) -> np.ndarray:
    out = np.zeros(round(total_s * R), np.float32)
    for t, sig in items:
        i = round(t * R)
        out[i : i + len(sig)] += sig[: len(out) - i]
    return out


def pcm16(x: np.ndarray) -> np.ndarray:
    return np.round(x * 32767).astype(np.int16)


def wav(name: str, x: np.ndarray, rate: int = R) -> None:
    OUT.mkdir(exist_ok=True)
    sf.write(OUT / name, np.clip(x, -1, 1), rate, subtype="FLOAT")


def tone(hz: float, s: float, dbfs: float) -> np.ndarray:
    t = np.arange(round(s * R)) / R
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * hz * t)
    ramp = min(len(x) // 2, R // 100)
    x[:ramp] *= np.linspace(0, 1, ramp)
    x[-ramp:] *= np.linspace(1, 0, ramp)
    return x


def bandpass_rms(x: np.ndarray, lo: float, hi: float) -> float:
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / R)
    spec[(f < lo) | (f > hi)] = 0
    return rms(np.fft.irfft(spec, len(x)))


# ---- experiments ----


def calibrate(dev: Dev, vol: float) -> dict:
    """Loop mode: the TX->RX frame offset K, and when a TX descriptor reaches the line after its
    predecessor's on_sent."""
    dev.cmd("vol -60", echo=False)  # the marker plays on the speaker too
    dev.cmd("gain 0", echo=False)  # bit-exact: no software gain
    dev.cmd("loop on", echo=False)
    buf = np.zeros(R // 10, np.int16)
    buf[1000], buf[1001] = 20000, -20000
    dev.load(buf)
    r = run(dev, 300, True)
    dev.cmd("loop off", echo=False)
    dev.cmd(f"vol {vol}", echo=False)
    (hits,) = np.nonzero(r.raw[:, 0] == 20000 << 16)
    assert len(hits) == 1 and r.raw[hits[0] + 1, 0] == -20000 << 16, hits
    k = int(r.frame0 + hits[0] - (r.tx_start + 1000))
    seq = r.tx_start // F
    lead_us = r.line_us(r.tx_start + k) - r.sent_us(seq - 1)
    ring_ms = (seq - r.info["tx_base"]) * F / R * 1e3  # not meaningful alone; see note
    out = {"K": k, "line_after_on_sent_us": lead_us, "tx_start": r.tx_start}
    print(f"CALIB K={k} frames; TX descriptor on the line {lead_us:.0f} us after its "
          f"predecessor's on_sent; slots bit-exact")
    del ring_ms
    return out


def exp_calib(dev: Dev, a) -> None:
    calibrate(dev, a.vol)


def exp_mic(dev: Dev, a) -> None:
    cal = calibrate(dev, a.vol)
    n = a.n
    dur = 1.0 + n + 0.6
    esp_at = [0.5 + i for i in range(n)]
    mouth_at = [1.0 + i for i in range(n)]
    esp = place(dur, [(t, DOWN * 0.5) for t in esp_at])
    mouth = place(dur, [(t, UP * a.mouth) for t in mouth_at])
    dev.load(pcm16(esp))
    with Pod(POD) as pod, Mouth(MOUTH) as m:
        r = run(dev, round(dur * 1000) + 600, True, during=lambda: m.play(mouth))
        time.sleep(0.4)
        heard = pod.heard()
    p = heard.signal.astype(np.float64)
    wav("mic_pod.wav", p)
    wav("mic_ch0_48k.wav", r.ch0)
    wav("mic_ch1_48k.wav", r.ch1)
    pe = find_all(p, DOWN, R // 4)
    pm = find_all(p, UP, R // 4)
    cm = find_all(r.ch0, UP, R // 4, thresh=0.3)
    ce = find_all(r.ch0, DOWN, R // 4, thresh=0.2)
    print(f"found: POD esp {len(pe)}/{n} mouth {len(pm)}/{n}; ch0 mouth {len(cm)}/{n} "
          f"(match {np.mean([x for _, x in cm]) if cm else 0:.2f}); ch0 esp residual chirps {len(ce)}")
    # Each ESP chirp on the line, as a capture index.
    line = [r.tx_start + round(t * R) + cal["K"] - r.frame0 for t in esp_at]
    lat = []
    for q, _ in pm:
        before = [pk for pk, _ in pe if 0 < q - pk < R]
        if not before:
            continue
        pk = before[-1]
        k = min(range(len(pe)), key=lambda i: abs(pe[i][0] - pk))
        ri = line[k] if k < len(line) else None
        # The mouth chirp on the line timeline: the ESP chirp's line time + (q - pk) at the POD.
        expect = ri + (q - pk)
        got = [c for c, _ in cm if 0 <= c - expect < R // 5]
        if got:
            lat.append((got[0] - expect) / R * 1e3)
    res = []
    for c, mt in ce:
        near = [c - li for li in line if 0 < c - li < R // 2]
        if near:
            res.append((near[0] / R * 1e3, mt))
    print("ch0 ESP-chirp residual matches, ms after the chirp hit the line (match): "
          + ", ".join(f"{d:.1f} ({mt:.2f})" for d, mt in res))
    pod_gap = [(q - pk) / R * 1e3 for (q, _), (pk, _) in zip(pm, pe)]
    print("POD mouth-after-esp gaps ms: " + ", ".join(f"{g:.1f}" for g in pod_gap))
    if lat:
        v = np.array(lat)
        print(f"MIC round trip (mic->I2S + speaker->air): n={len(v)} mean={v.mean():.2f} "
              f"min={v.min():.2f} max={v.max():.2f} sd={v.std():.2f} ms")
    # Sanity of the mic stream.
    spec = np.abs(np.fft.rfft(r.ch0)) ** 2
    f = np.fft.rfftfreq(len(r.ch0), 1 / R)
    hi = spec[f > 8200].sum() / max(spec[(f > 100) & (f < 7800)].sum(), 1e-30)
    eq = np.mean(r.raw[1:, 0] == r.raw[:-1, 0])
    trip = [np.mean(r.raw[ph + 1 :: 3, 0][: len(r.raw) // 3 - 1] == r.raw[ph::3, 0][: len(r.raw) // 3 - 1]) for ph in range(3)]
    print(f"ch0: rms {db(rms(r.ch0)):.1f} dBFS, energy over 8.2 kHz {10*np.log10(hi+1e-30):.1f} dB "
          f"re 0.1-7.8 kHz; equal neighbours {eq:.3f} (by phase {', '.join(f'{t:.3f}' for t in trip)})")
    print(f"ch1: rms {db(rms(r.ch1)):.1f} dBFS, low 16 bits of ch0 used: {np.mean((r.raw[:,0] & 0xFFFF) != 0):.3f}")
    c = np.correlate(r.ch0[: R * 4], r.ch1[: R * 4], "valid") if False else None
    del c
    if rms(r.ch1) > 0:
        x0, x1 = r.ch0 - r.ch0.mean(), r.ch1 - r.ch1.mean()
        size = 1 << (2 * len(x0)).bit_length()
        xc = np.fft.irfft(np.fft.rfft(x0, size) * np.conj(np.fft.rfft(x1, size)), size)
        xc = np.concatenate((xc[-R // 20 :], xc[: R // 20]))
        best = int(np.argmax(np.abs(xc)))
        print(f"ch0~ch1 correlation: {np.abs(xc).max() / (np.linalg.norm(x0) * np.linalg.norm(x1)):.3f} "
              f"at lag {(best - R // 20) / R * 1e3:.2f} ms")
    json.dump({"cal": cal, "lat_ms": lat}, open(OUT / "mic.json", "w"))


def pod_level(heard_sig: np.ndarray, start: int, stop: int, lo=200.0, hi=4000.0) -> float:
    return db(bandpass_rms(heard_sig[start:stop], lo, hi))


def envelope_db(x: np.ndarray, hop: int) -> np.ndarray:
    n = len(x) // hop
    return np.array([db(rms(x[i * hop : (i + 1) * hop])) for i in range(n)])


def exp_aec(dev: Dev, a) -> None:
    """The ESP's own speech in ch0 (after the XU316's AEC/NS/AGC), once converged, against the
    same speech from the mouth at the same level at the POD; a silence floor; double talk."""
    cal = calibrate(dev, a.vol)
    en, ru = speech("en.wav", 0.9), speech("ru.wav", 0.9)
    sa = np.concatenate([en, ru])[: 12 * R]  # A: the ESP's, and the mouth's for the comparison
    sb = np.concatenate([ru, en])[: 12 * R]  # B: the mouth's in double talk
    # Level match on A's first 3.8 s: the ESP by software gain, the mouth at peak 0.9.
    probe_esp = place(9.0, [(0.3, en)])
    dev.cmd("gain 0", echo=False)
    dev.load(pcm16(probe_esp))
    with Pod(POD) as pod, Mouth(MOUTH) as m:
        run(dev, 9000, True, during=lambda: (time.sleep(4.6), m.play(en)))
        time.sleep(0.3)
        p = pod.heard().signal.astype(np.float64)
    env = np.convolve(p**2, np.ones(R // 10) / (R // 10), "same")

    def loudest(lo, hi, length):
        e = np.convolve(env[lo:hi], np.ones(length), "valid")
        i = int(np.argmax(e)) + lo
        return i, i + length

    first = loudest(0, len(p) // 2, len(en))
    second = loudest(first[1], len(p), len(en))
    le, lm = pod_level(p, *first, 100, 7800), pod_level(p, *second, 100, 7800)
    g = lm - le
    print(f"level match: POD esp {le:.1f} dB, mouth {lm:.1f} dB -> ESP software gain {g:+.1f} dB")
    dev.cmd(f"gain {g:.1f}", echo=False)

    res = {}
    plans = {
        "silence": ([], []),
        "mouth_A": ([], [(1.0, sa)]),
        "esp_A": ([(1.0, sa)], []),
        "esp_A_again": ([(1.0, sa)], []),
        "double_talk": ([(1.0, sa)], [(1.0, sb)]),
    }
    for name, (esp_items, mouth_items) in plans.items():
        if a.args and name not in a.args:
            continue
        length = 13.6 if name != "silence" else 4.0
        esp = place(length, [(0.25, DOWN * 0.3)] + esp_items)
        mouth = place(length, [(0.6, UP * 0.9)] + mouth_items)
        dev.load(pcm16(esp))
        with Pod(POD) as pod, Mouth(MOUTH) as m:
            r = run(dev, round(length * 1000), True, during=lambda: m.play(mouth))
            time.sleep(0.3)
            p = pod.heard().signal.astype(np.float64)
        # The line index of the ESP's marker, and through the POD, of the mouth's.
        r_esp = r.tx_start + round(0.25 * R) + cal["K"] - r.frame0
        pe, pm = find_all(p, DOWN, R // 4), find_all(p, UP, R // 4)
        mouth_off = (pm[0][0] - pe[0][0]) if pe and pm else round(0.35 * R)
        r_mouth = r_esp + mouth_off  # in the air at the mics, on the line's timeline
        lat = round(0.1205 * R)  # mic -> I2S, measured by `mic`
        # A's items in ch0: from 1.0 s after each marker's time, delayed by the mic path.
        esp_s = r_esp + round(0.75 * R) + lat
        mouth_s = r_mouth + round(0.4 * R) + lat
        s0 = esp_s if esp_items else mouth_s if mouth_items else r_esp + round(1.2 * R) + lat
        dur = len(sa) if (esp_items or mouth_items) else 2 * R
        x0, x1 = r.ch0[s0 : s0 + dur], r.ch1[s0 : s0 + dur]
        tail = slice(dur - 4 * R, dur) if dur > 4 * R else slice(0, dur)
        out = {
            "ch0_db": db(bandpass_rms(x0, 100, 7800)),
            "ch0_tail_db": db(bandpass_rms(x0[tail], 100, 7800)),
            "ch1_db": db(bandpass_rms(x1, 100, 7800)),
            "curve": [round(float(v), 1) for v in envelope_db(x0, R // 2)],
        }
        # POD, over the same stretch of air (the line minus the mic path).
        pod0 = (pe[0][0] if pe else 0) + (s0 - lat - r_esp)
        out["pod_db"] = db(bandpass_rms(p[max(0, pod0) : pod0 + dur], 100, 7800))
        if name == "double_talk":
            hop = R // 50
            ea, eb = envelope_db(sa, hop), envelope_db(sb, hop)
            e0a = envelope_db(r.ch0[esp_s : esp_s + len(sa)], hop)
            e0b = envelope_db(r.ch0[mouth_s : mouth_s + len(sb)], hop)
            n = min(len(ea), len(eb), len(e0a), len(e0b))
            ok = (ea[:n] > -50) | (eb[:n] > -50)
            out["corr_with_mouth_B"] = float(np.corrcoef(e0b[:n][ok], eb[:n][ok])[0, 1])
            out["corr_with_esp_A"] = float(np.corrcoef(e0a[:n][ok], ea[:n][ok])[0, 1])
            # The same correlation for the mouth alone, as the ceiling.
            out["mouth_B_only_ref"] = None
        res[name] = out
        wav(f"aec_{name}_ch0.wav", r.ch0)
        wav(f"aec_{name}_pod.wav", p)
        print(f"{name:12s} POD {out['pod_db']:6.1f}  ch0 {out['ch0_db']:6.1f} (last 4 s {out['ch0_tail_db']:6.1f})"
              f"  ch1 {out['ch1_db']:6.1f}  by 0.5 s {out['curve']}")
        if name == "double_talk":
            print(f"   ch0 envelope correlates {out['corr_with_mouth_B']:.2f} with the mouth's B, "
                  f"{out['corr_with_esp_A']:.2f} with the ESP's A")
    if not {"mouth_A", "esp_A_again", "silence"} <= res.keys():
        return
    near, echo = res["mouth_A"], res["esp_A_again"]
    print(f"AEC: converged own speech sits {near['ch0_tail_db'] - echo['ch0_tail_db']:.1f} dB under the "
          f"same speech from outside (POD {echo['pod_db']:.1f} vs {near['pod_db']:.1f}); "
          f"{echo['ch0_tail_db'] - res['silence']['ch0_db']:.1f} dB over the silence floor")
    json.dump(res, open(OUT / "aec.json", "w"), indent=1)
    dev.cmd("gain 0", echo=False)


def exp_hf(dev: Dev, a) -> None:
    """Does playback bypass the XU316's 16 kHz pipeline? A 12 kHz tone at the POD."""
    dev.cmd(f"vol {a.vol}", echo=False)
    sig = place(4.5, [(0.3, tone(1000, 1.5, -12)), (2.3, tone(12000, 1.5, -12))])
    dev.load(pcm16(sig))
    with Pod(POD) as pod:
        r = run(dev, 4600, True)
        time.sleep(0.2)
        p = pod.heard().signal.astype(np.float64)
    l1 = db(float(np.median(tone_level(p, R, 1000))))
    l12 = db(float(np.median(tone_level(p, R, 12000)[np.argmax(tone_level(p, R, 12000)) - R // 2 :][: R])))
    seg1 = slice(r.tx_start - r.frame0 + round(0.6 * R), r.tx_start - r.frame0 + round(1.5 * R))
    seg12 = slice(r.tx_start - r.frame0 + round(2.6 * R), r.tx_start - r.frame0 + round(3.5 * R))
    print(f"POD: 1 kHz peak level {db(float(tone_level(p, R, 1000).max())):.1f} dB, 12 kHz peak "
          f"{db(float(tone_level(p, R, 12000).max())):.1f} dB (median-1k {l1:.1f}, {l12:.1f})")
    print(f"ch0 at 12 kHz while it plays: {db(bandpass_rms(r.ch0[seg12], 11500, 12500)):.1f} dBFS; "
          f"at 1 kHz while 1 kHz plays: {db(bandpass_rms(r.ch0[seg1], 900, 1100)):.1f} dBFS")
    wav("hf_pod.wav", p)


def steps_run(dev: Dev, cmds: list[str], hold: float) -> tuple[Run, np.ndarray, list[float]]:
    with Pod(POD) as pod:
        def during():
            for c in cmds:
                time.sleep(hold)
                dev.cmd(c, echo=False)
        ms = round((len(cmds) + 1.5) * hold * 1000)
        r = run(dev, ms, False, during=during)
        p = pod.heard().signal.astype(np.float64)
    return r, p, []


def transitions(level: np.ndarray, min_db: float = 2.0, gap: int = R // 4) -> list[int]:
    ld = 20 * np.log10(np.maximum(level, 1e-9))
    sm = np.convolve(ld, np.ones(R // 50) / (R // 50), "same")
    d = sm[R // 25 :] - sm[: -R // 25]
    out: list[int] = []
    for i in np.argsort(-np.abs(d)):
        if abs(d[i]) < min_db:
            break
        if all(abs(int(i) - o) > gap for o in out):
            out.append(int(i) + R // 50)
    return sorted(out)


def exp_vol(dev: Dev, a) -> None:
    """Volume by the AIC3204's DAC digital gain against software gain: the steps at the POD,
    clicks at each step, and ch0 (the AEC) after each step."""
    steps = [0, -6, -12, -18, -24, 0, -0.5, -1.0, -1.5]
    for method in ("aic", "sw"):
        dev.cmd("vol 0", echo=False)
        dev.cmd("gain 0", echo=False)
        dev.cmd("tone 1000 -30", echo=False)  # the POD saturates near -4 dB: keep it under -15
        time.sleep(1.0)
        cmds = [f"vol {s}" if method == "aic" else f"gain {s}" for s in steps]
        r, p, _ = steps_run(dev, cmds, 0.8)
        dev.cmd("stop", echo=False)
        dev.cmd("vol 0", echo=False)
        dev.cmd("gain 0", echo=False)
        lv = tone_level(p, R, 1000)
        tr = [t for t in transitions(lv, 0.3) if R // 2 < t < len(p) - R // 2]
        print(f"{method}: {len(tr)} level steps found at the POD")
        prev = None
        for t in tr:
            before = db(float(np.median(lv[max(0, t - R // 3) : t - R // 50])))
            after = db(float(np.median(lv[t + R // 20 : t + R // 3])))
            c = click_db(p, R, t - R // 100, (max(0, t - R // 2), t - R // 20), hz=1000)
            print(f"  step {after - before:+6.2f} dB (to {after:6.1f})  click {c:5.1f} dB")
            prev = after
        del prev
        wav(f"vol_{method}_pod.wav", p)
        wav(f"vol_{method}_ch0.wav", r.ch0)


def exp_volaec(dev: Dev, a) -> None:
    """The AEC across a volume step, with speech: the XU316's reference is the I2S TX, so a step
    in the AIC3204 (after the reference) changes the echo path, while software gain doesn't.
    ch0 by 0.5 s; the step down 12 dB at ~6 s into the speech, back up at ~9.5 s."""
    cal = calibrate(dev, a.vol)
    en, ru = speech("en.wav", 0.9), speech("ru.wav", 0.9)
    sa = np.concatenate([en, ru])[: 12 * R]
    length = 13.6
    dev.load(pcm16(place(length, [(0.25, DOWN * 0.3), (1.0, sa)])))
    dev.cmd(f"vol {a.vol}", echo=False)
    dev.cmd(f"gain {a.gain}", echo=False)
    lat = round(0.1205 * R)
    res = {}
    for name in ("warmup", "none", "sw", "none_again", "aic", "after_aic", "after_aic2"):
        down = {"aic": f"vol {a.vol - 12}", "sw": f"gain {a.gain - 12}"}.get(name)
        up = {"aic": f"vol {a.vol}", "sw": f"gain {a.gain}"}.get(name)

        def during():
            if down:
                time.sleep(7.0)
                dev.cmd(down, echo=False)
                time.sleep(3.5)
                dev.cmd(up, echo=False)

        r = run(dev, round(length * 1000), True, during=during)
        s0 = r.tx_start + round(1.0 * R) + cal["K"] - r.frame0 + lat
        x0 = r.ch0[s0 : s0 + len(sa)]
        curve = [round(float(v), 1) for v in envelope_db(x0, R // 2)]
        res[name] = curve
        wav(f"volaec_{name}_ch0.wav", r.ch0)
        print(f"{name:10s} ch0 by 0.5 s: {curve}")
    for name in ("sw", "aic", "after_aic", "after_aic2"):
        d = [round(x - y, 1) for x, y in zip(res[name], res["none_again"])]
        print(f"{name:10s} minus none_again: {d}")
    json.dump(res, open(OUT / f"volaec_v{a.vol}_g{a.gain}.json", "w"), indent=1)
    dev.cmd(f"vol {a.vol}", echo=False)
    dev.cmd("gain 0", echo=False)


def exp_aecab(dev: Dev, a) -> None:
    """Which of exp_aec's trappings volaec lacks matters: the mouth's chirp at 0.6 s, the POD
    and mouth streams open, a reload before each run."""
    cal = calibrate(dev, a.vol)
    en, ru = speech("en.wav", 0.9), speech("ru.wav", 0.9)
    sa = np.concatenate([en, ru])[: 12 * R]
    length = 13.6
    esp = pcm16(place(length, [(0.25, DOWN * 0.3), (1.0, sa)]))
    dev.load(esp)
    dev.cmd(f"vol {a.vol}", echo=False)
    dev.cmd(f"gain {a.gain}", echo=False)
    lat = round(0.1205 * R)
    chirped = place(length, [(0.6, UP * 0.9)])
    silent = np.zeros(round(length * R))
    for name in a.args or ["bare", "streams", "chirp", "bare", "reload", "chirp", "streams"]:
        if name == "reload":
            dev.load(esp)
        if name in ("streams", "chirp"):
            with Pod(POD) as pod, Mouth(MOUTH) as m:
                r = run(dev, round(length * 1000), True,
                        during=lambda: m.play(chirped if name == "chirp" else silent))
                time.sleep(0.3)
        else:
            r = run(dev, round(length * 1000), True)
        s0 = r.tx_start + round(1.0 * R) + cal["K"] - r.frame0 + lat
        x0 = r.ch0[s0 : s0 + len(sa)]
        c = envelope_db(x0, R // 2)
        print(f"{name:8s} ch0 {db(bandpass_rms(x0, 100, 7800)):6.1f} dB, last 4 s "
              f"{db(bandpass_rms(x0[-4 * R:], 100, 7800)):6.1f}; by 0.5 s {[round(float(v), 1) for v in c]}")
    dev.cmd("gain 0", echo=False)


def exp_cutair(dev: Dev, a) -> None:
    """Tone cuts at the POD: the DMA-patch cut against a plain stop (the TX source going silent
    at a descriptor boundary)."""
    dev.cmd(f"vol {a.vol}", echo=False)
    dev.cmd(f"lead {a.lead}", echo=False)
    for kind, c in (("cut", "cut"), ("stop", "stop")):
        times = []
        with Pod(POD) as pod:
            for _ in range(a.n):
                dev.cmd("tone 1000 -6", echo=False)
                time.sleep(0.4 + np.random.rand() * 0.2)
                times.append(time.time() * 1000)
                dev.cmd(c, echo=False)
                time.sleep(0.3)
            heard = pod.heard()
        p = heard.signal.astype(np.float64)
        lv = tone_level(p, R, 1000)
        tr = [t for t in transitions(lv, 10.0) if R // 4 < t < len(lv) - R // 10 and lv[t + R // 20] < lv[t - R // 20]]
        clicks = [click_db(p, R, t - R // 200, (max(0, t - R // 4), t - R // 100), hz=1000) for t in tr]
        print(f"{kind}: {len(tr)} ends found, click dB: {', '.join(f'{c:.1f}' for c in clicks)}")
        wav(f"cutair_{kind}_pod.wav", p)


def exp_boot(dev: Dev, a) -> None:
    """XU316 resets from GPIO2: WS back, I2C answering, the mic's first sample; the POD listens
    for pops."""
    with Pod(POD) as pod:
        for _ in range(a.n):
            dev.cmd("xrst", timeout=40)
            dev.cmd("ver")
            time.sleep(0.5)
        p = pod.heard().signal.astype(np.float64)
    env = np.sqrt(np.convolve(p**2, np.ones(R // 1000) / (R // 1000), "same"))
    print(f"POD over the resets: floor {db(float(np.percentile(env, 50))):.1f} dB, peak {db(float(env.max())):.1f} dB")
    wav("boot_pod.wav", p)


def exp_romlog(dev: Dev, a) -> None:
    """The ROM banner on GPIO43 at the speaker: software reboots with and without
    rtc_suppress_rom_log, a USB-Serial/JTAG reset, and (with --flash) flashing."""
    events = []

    def reset_usj() -> None:
        dev.s.dtr = False  # RTS=1, DTR=0: chip reset
        time.sleep(0.05)
        dev.s.dtr = True

    plan = [
        ("reboot, ROM log on", lambda: (dev.cmd("romlog on", echo=False), dev.s.write(b"reboot\n"))),
        ("reboot, ROM log off", lambda: (dev.cmd("romlog off", echo=False), dev.s.write(b"reboot\n"))),
        ("USJ reset, ROM log off", reset_usj),
        ("USJ reset, ROM log on", lambda: (dev.cmd("romlog on", echo=False), reset_usj())),
        ("reboot, amp off first", lambda: (dev.cmd("spk 0", echo=False), dev.s.write(b"reboot\n"))),
    ]
    with Pod(POD) as pod:
        time.sleep(1.0)
        for name, act in plan:
            mark = len(pod.heard().signal)
            act()
            time.sleep(4.0)
            try:
                dev.s.close()
            except Exception:
                pass
            for _ in range(20):
                try:
                    dev.s.open()
                    break
                except Exception:
                    time.sleep(0.3)
            time.sleep(0.5)
            dev.s.reset_input_buffer()
            if "amp off" in name:
                dev.cmd("spk 1", echo=False)
            events.append((name, mark))
        if a.flash:
            dev.s.close()
            mark = len(pod.heard().signal)
            subprocess.run([str(Path.home() / ".platformio/penv/bin/pio"), "run", "-d",
                            str(Path(__file__).resolve().parent.parent), "-t", "upload",
                            "--upload-port", dev.s.port], check=True, capture_output=True)
            time.sleep(3)
            events.append(("flash", mark))
        time.sleep(0.5)
        p = pod.heard().signal.astype(np.float64)
    env = np.sqrt(np.convolve(p**2, np.ones(R // 1000) / (R // 1000), "same"))
    floor = db(float(np.percentile(env[: R], 50)))
    for i, (name, mark) in enumerate(events):
        end = events[i + 1][1] if i + 1 < len(events) else len(p)
        seg = env[mark:end]
        loud = np.nonzero(seg > 10 ** ((floor + 20) / 20))[0]
        span = (loud[-1] - loud[0]) / R * 1e3 if len(loud) else 0
        print(f"{name:24s} peak {db(float(seg.max())):6.1f} dB (floor {floor:.1f}), >20 dB over floor "
              f"for {len(loud) / R * 1e3:.0f} ms within {span:.0f} ms")
    wav("romlog_pod.wav", p)


def exp_listen(dev: Dev, a) -> None:
    """For the ear: speech on the JST speaker, then ramped cuts against hard stops."""
    en, ru = speech("en.wav", 0.9), speech("ru.wav", 0.9)
    dev.cmd(f"vol {a.vol}", echo=False)
    dev.cmd("gain 0", echo=False)
    dev.load(pcm16(place(12.5, [(0.3, np.concatenate([en, ru])[: 12 * R])])))
    print(">>> speech, 12 s")
    run(dev, 12500, True)
    for kind in ("cut", "stop", "cut", "stop", "cut", "stop"):
        print(f">>> tone, then {'RAMPED CUT' if kind == 'cut' else 'HARD STOP'}")
        dev.cmd("tone 1000 -20", echo=False)
        time.sleep(1.2)
        dev.cmd(kind, echo=False)
        time.sleep(1.5)
    dev.cmd("vol 0", echo=False)


def exp_poweron(dev: Dev, a) -> None:
    """Power-on noise at the speaker: the POD listens while the board is unplugged and
    replugged (the RTC bit that hides the ROM log doesn't survive power-off)."""
    dev.s.close()
    with Pod(POD) as pod:
        time.sleep(1.0)
        print(">>> unplug the ReSpeaker, wait 2 s, plug it back in (25 s)", flush=True)
        time.sleep(25)
        p = pod.heard().signal.astype(np.float64)
    env = np.sqrt(np.convolve(p**2, np.ones(R // 1000) / (R // 1000), "same"))
    floor = db(float(np.percentile(env[: R], 50)))
    loud = np.nonzero(env > 10 ** ((floor + 20) / 20))[0]
    print(f"power cycle: peak {db(float(env.max())):.1f} dB (floor {floor:.1f}), >20 dB over floor for "
          f"{len(loud) / R * 1e3:.0f} ms" + (f", first at {loud[0] / R:.2f} s, last {loud[-1] / R:.2f} s" if len(loud) else ""))
    wav("poweron_pod.wav", p)


def exp_load(dev: Dev, a) -> None:
    """Headroom: WiFi up, full-duplex UDP audio at 16 kHz (speaker in through the 3x FIR, mic
    out as every 3rd sample), then CPU per task and heap."""
    lines = dev.cmd("wifi", timeout=30)
    ip = [ln.split("ip=")[1] for ln in lines if "ip=" in ln]
    if not ip:
        raise SystemExit("no WiFi")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 9001))
    sock.settimeout(0.2)
    got = []
    stop = threading.Event()

    def send() -> None:
        t = np.arange(320) / 16000
        n = 0
        nxt = time.perf_counter()
        while not stop.is_set():
            frame = (0.25 * 32767 * np.sin(2 * np.pi * 440 * (t + n * 0.02))).astype("<i2")
            sock.sendto(frame.tobytes(), (ip[0], 9000))
            n += 1
            nxt += 0.02
            time.sleep(max(0, nxt - time.perf_counter()))

    def recv() -> None:
        while not stop.is_set():
            try:
                got.append(sock.recv(2048))
            except TimeoutError:
                pass

    ts = [threading.Thread(target=send), threading.Thread(target=recv)]
    for t in ts:
        t.start()
    dev.cmd(f"vol {a.vol}", echo=False)
    dev.cmd(f"udp {a.host} 9001")
    time.sleep(3)
    dev.cmd("top 5000", timeout=20)
    dev.cmd("info")
    stop.set()
    for t in ts:
        t.join()
    dev.cmd("udp off")
    mic = np.frombuffer(b"".join(got), "<i2")
    print(f"host got {len(got)} mic frames ({len(mic) / 16000:.1f} s), rms {db(rms(mic / 32768)):.1f} dBFS")
    wav("load_mic16k.wav", mic / 32768, 16000)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--vol", type=float, default=0.0, help="AIC3204 DAC volume, dB")
    ap.add_argument("--gain", type=float, default=0.0, help="base software gain, dB (volaec)")
    ap.add_argument("--mouth", type=float, default=0.3, help="mouth amplitude")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--lead", type=int, default=28)
    ap.add_argument("--host", default="192.168.0.106")
    ap.add_argument("--flash", action="store_true")
    ap.add_argument("exp")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    dev = Dev(a.port)
    if a.exp == "con":
        for c in a.args:
            dev.cmd(c, timeout=120)
        return
    globals()[f"exp_{a.exp}"](dev, a)


if __name__ == "__main__":
    main()
