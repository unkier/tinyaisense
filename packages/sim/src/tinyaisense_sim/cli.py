"""`tinyaisense-sim`: simulated devices in a terminal, against a hub. `KEYS` work the
selected device."""

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import sys
import termios
import time
import tty
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from tinyaisense_wire import WireError

from . import pc
from .faults import GilbertElliott
from .led import Light
from .mic import SimMic
from .profiles import PROFILES, Board, from_manifest
from .simulator import SimDevice, Simulator
from .sinks import Null, Sink, Wav

PROG = "tinyaisense-sim"
ECHO = "m5stack-atom-echo"
FPS = 25
"""How often the status lines are redrawn, for the LEDs' patterns."""
KEYS = (
    "space press/release · c click · d double click · l long press · Tab next device · "
    "r RST · v vanish · w WiFi down/up · b reboot · q quit"
)


@dataclass(frozen=True, slots=True)
class _DeviceArg:
    """A device to run, as `--device` or `--manifest` gives it."""

    board: Board
    name: str | None


type _SinkFor = Callable[[str], Sink]
"""A new sink for the speaker named `<device>`, or `<device>-<cap>` on a Board with several."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.devices and args.count is not None:
        parser.error("give --count, or --device and --manifest, not both")
    devices: list[_DeviceArg] = args.devices or [_DeviceArg(PROFILES[ECHO], None)] * (
        args.count or 1
    )
    verbose: int = args.verbose
    level = [logging.WARNING, logging.INFO, logging.DEBUG][min(verbose, 2)]
    failed = False
    try:
        asyncio.run(_run(args, devices, level))
    except* pc.NoAudio as group:
        for e in group.exceptions:
            print(f"{PROG}: {e}", file=sys.stderr)
        failed = True
    return 1 if failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Simulated tinyaisense devices against a hub.", epilog=KEYS
    )
    parser.add_argument(
        "--count", type=_positive, help="run this many Atom Echos, named sim-1 on (default 1)"
    )
    parser.add_argument(
        "--device",
        dest="devices",
        action="append",
        type=_device,
        metavar="BOARD[:NAME]",
        help=f"run a device of BOARD, one of {', '.join(PROFILES)}; repeat for more",
    )
    parser.add_argument(
        "--manifest",
        dest="devices",
        action="append",
        type=_manifest,
        metavar="FILE",
        help="run a device with the manifest in JSON FILE, less protocol, device_id and "
        "firmware; repeat for more",
    )
    parser.add_argument(
        "--hub", type=_hub, metavar="HOST:PORT", help="the hub's device port (default: mDNS)"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        metavar="DIR",
        default=_state_dir(),
        help="where the device tokens live, so that pairing lasts (default %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="log more: -vv logs every message"
    )
    parser.add_argument("--seed", type=int, default=0, help="for the faults' randomness")
    parser.add_argument(
        "--trace", type=Path, metavar="FILE", help="write every device's timeline to JSONL FILE"
    )
    parser.add_argument(
        "--sink",
        type=_sink,
        default="null",
        metavar="SINK",
        help="where the speakers' audio goes: null (default), wav:DIR (DIR/<device>.wav) or "
        "pc (needs the [audio] extra)",
    )
    parser.add_argument(
        "--mic",
        choices=["pc"],
        help="what the mics hear while they stream (default: silence); pc: the PC's mic",
    )
    faults = parser.add_argument_group("faults, on every device")
    faults.add_argument("--loss", type=_percent, default=0, metavar="PCT", help="UDP loss")
    faults.add_argument(
        "--bursts",
        type=_bursts,
        metavar="P:R",
        help="bursty UDP loss: the %% chances, per datagram, of a burst starting and ending",
    )
    faults.add_argument("--delay", type=float, default=0, metavar="MS", help="UDP delay")
    faults.add_argument(
        "--jitter", type=float, default=0, metavar="MS", help="up to this much more UDP delay"
    )
    faults.add_argument(
        "--duplicate", type=_percent, default=0, metavar="PCT", help="UDP duplicates"
    )
    faults.add_argument(
        "--drift-ppm", type=float, default=0, metavar="PPM", help="the crystal runs this fast"
    )
    faults.add_argument(
        "--delay-replies", type=int, default=0, metavar="MS", help="hold each reply this long"
    )
    faults.add_argument(
        "--delay-events", type=int, default=0, metavar="MS", help="hold each Event this long"
    )
    return parser


async def _run(args: argparse.Namespace, devices: list[_DeviceArg], level: int) -> None:
    # Undone in reverse, each whatever the others do: the terminal is always given back.
    async with contextlib.AsyncExitStack() as stack:
        console = _Console(sys.stdout, sys.stderr)
        stack.callback(console.close)
        handler = _ConsoleHandler(console)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(level=level, handlers=[handler], force=True)
        sim = Simulator(args.hub, args.state_dir, seed=args.seed, trace=args.trace)
        ui = _Ui(sim, console)
        stack.callback(ui.close)
        stack.push_async_callback(sim.close)
        if args.mic == "pc":
            stack.callback(pc.PcMic(ui.mics).close)
        async with asyncio.TaskGroup() as tasks:
            adding = [
                tasks.create_task(_add(sim, device.board, device.name or f"sim-{i}", args))
                for i, device in enumerate(devices, 1)
            ]
            await ui.run()
            for task in adding:  # those the hub hasn't answered yet
                task.cancel()


async def _add(sim: Simulator, board: Board, name: str, args: argparse.Namespace) -> None:
    """Run a device, and once the hub has answered it, apply the flags' faults and sink."""
    device = await sim.add(board, name)
    for way in (device.faults.udp_down, device.faults.udp_up):
        way.loss = args.loss
        way.bursts = args.bursts
        way.delay_ms = args.delay
        way.jitter_ms = args.jitter
        way.duplicate = args.duplicate
    device.faults.drift_ppm = args.drift_ppm
    device.faults.delay_replies_ms = args.delay_replies
    device.faults.delay_events_ms = args.delay_events
    sink_for: _SinkFor = args.sink
    speakers = device.speakers
    for cap, speaker in speakers.items():
        speaker.sink = sink_for(name if len(speakers) == 1 else f"{name}-{cap}")


class _Console:
    """The status lines, redrawn in place on a terminal, with the log lines going above them.
    Off a terminal, a device's line is printed each time it changes, without its LEDs."""

    def __init__(self, out: TextIO, err: TextIO) -> None:
        self.live = out.isatty()
        self._out = out
        self._err = err
        self._lines: list[str] = []
        if self.live:
            out.write("\x1b[?25l\x1b[?7l")  # no cursor, no wrapping: one line a device

    def draw(self, lines: list[str]) -> None:
        if lines == self._lines:
            return
        if self.live:
            self._out.write(self._clear() + "".join(f"{line}\n" for line in lines))
        else:
            old = set(self._lines)
            self._out.write("".join(f"{line}\n" for line in lines if line not in old))
        self._lines = lines
        self._out.flush()

    def log(self, text: str) -> None:
        if self.live:
            self._out.write(self._clear())
            self._out.flush()
        self._err.write(f"{text}\n")
        self._err.flush()
        if self.live:
            self._out.write("".join(f"{line}\n" for line in self._lines))
            self._out.flush()

    def close(self) -> None:
        if self.live:
            self._out.write("\x1b[?7h\x1b[?25h")
            self._out.flush()

    def _clear(self) -> str:
        """Up to the first status line, and clear to the end of the screen."""
        return f"\x1b[{len(self._lines)}F\x1b[J" if self._lines else ""


class _ConsoleHandler(logging.Handler):
    def __init__(self, console: _Console) -> None:
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._console.log(self.format(record))
        except Exception:
            self.handleError(record)


class _Ui:
    """The keys in, the status lines out."""

    def __init__(self, sim: Simulator, console: _Console) -> None:
        self._sim = sim
        self._console = console
        self._selected = 0
        self._quit = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stdin = sys.stdin.fileno()
        self._termios: Any = None
        if os.isatty(self._stdin):
            self._termios = termios.tcgetattr(self._stdin)
            tty.setcbreak(self._stdin)
        console.log(KEYS)

    def mics(self) -> list[SimMic]:
        return [mic for device in self._sim.devices for mic in device.mics.values()]

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._quit.set)
        if self._termios is not None:
            loop.add_reader(self._stdin, self._keys)
        try:
            while not self._quit.is_set():
                self._draw()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._quit.wait(), 1 / FPS)
        finally:
            if self._termios is not None:
                loop.remove_reader(self._stdin)
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            for task in self._tasks:
                task.cancel()

    def close(self) -> None:
        """Give the terminal back as it was."""
        if self._termios is not None:
            termios.tcsetattr(self._stdin, termios.TCSADRAIN, self._termios)

    def _draw(self) -> None:
        now = time.monotonic()
        devices = self._sim.devices
        self._console.draw(
            [
                _line(device, i == self._selected, now if self._console.live else None)
                for i, device in enumerate(devices)
            ]
        )

    def _keys(self) -> None:
        for key in os.read(self._stdin, 64).decode(errors="ignore"):
            self._key(key)

    def _key(self, key: str) -> None:
        devices = self._sim.devices
        if key == "q":
            self._quit.set()
        if not devices:
            return
        if key == "\t":
            self._selected = (self._selected + 1) % len(devices)
            return
        device = devices[min(self._selected, len(devices) - 1)]
        button = next(iter(device.buttons.values()), None)
        match key:
            case " " if button is not None:
                if button.pressed:
                    button.release()
                else:
                    button.press()
            case "c" if button is not None:
                self._spawn(button.click())
            case "d" if button is not None:
                self._spawn(button.double_click())
            case "l" if button is not None:
                self._spawn(button.long_press())
            case "r":
                device.faults.rst()
            case "v":
                device.faults.vanish()
            case "w":
                if device.faults.wifi_down:
                    device.faults.wifi_down = False
                else:
                    device.faults.wifi_drop()
            case "b":
                device.faults.reboot()
            case _:
                pass
        self._draw()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _line(device: SimDevice, selected: bool, now: float | None) -> str:
    """`device`'s status line: its LEDs as they look at `now`, or not at all without one."""
    leds = (
        ""
        if now is None
        else "".join(
            _swatch(light, now)
            for cap in device.manifest.supported
            if cap.kind == "led"
            for light in device.lights(cap.id)
        )
        + " "
    )
    extras = [
        *(f"{cap} down" for cap, button in device.buttons.items() if button.pressed),
        *(f"{cap} {s.stream}" for cap, s in device.speakers.items() if s.stream is not None),
        *(f"{cap} {m.stream}" for cap, m in device.mics.items() if m.stream is not None),
        *(["wifi down"] if device.faults.wifi_down else []),
        *([f"reconnects {device.reconnects}"] if device.reconnects else []),
    ]
    mark = ">" if selected else " "
    status = f"{device.state:<10} {device.status:<10}"
    return f"{mark} {device.instance:<12} {leds}{status} {'  '.join(extras)}".rstrip()


def _swatch(light: Light, now: float) -> str:
    """`light` at `now` as a colored block, in 24-bit color."""
    phase = now * 1000 % light.period_ms / light.period_ms
    match light.pattern:
        case "solid":
            on = 1.0
        case "blink":
            on = 1.0 if phase < 0.5 else 0.0
        case "pulse":
            on = (1 - math.cos(2 * math.pi * phase)) / 2
    on *= math.sqrt(light.level / 255)  # so a dim LED still shows on a terminal
    r, g, b = (round(int(light.color[i : i + 2], 16) * on) for i in (1, 3, 5))
    return f"\x1b[38;2;{r};{g};{b}m██\x1b[0m"


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"{n} isn't positive")
    return n


def _percent(value: str) -> float:
    pct = float(value)
    if not 0 <= pct <= 100:
        raise argparse.ArgumentTypeError(f"{pct} isn't a percentage")
    return pct / 100


def _bursts(value: str) -> GilbertElliott:
    p, sep, r = value.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(f"{value!r} isn't P:R")
    return GilbertElliott(_percent(p), _percent(r))


def _device(value: str) -> _DeviceArg:
    board, _, name = value.partition(":")
    if board not in PROFILES:
        raise argparse.ArgumentTypeError(f"unknown board {board!r}; known: {', '.join(PROFILES)}")
    return _DeviceArg(PROFILES[board], name or None)


def _manifest(value: str) -> _DeviceArg:
    try:
        return _DeviceArg(from_manifest(json.loads(Path(value).read_text())), None)
    except (OSError, ValueError, WireError) as e:
        raise argparse.ArgumentTypeError(f"{value}: {e}") from e


def _hub(value: str) -> str:
    host, sep, port = value.rpartition(":")
    if not (sep and host and port.isdigit()):
        raise argparse.ArgumentTypeError(f"{value!r} isn't HOST:PORT")
    return value


def _sink(value: str) -> _SinkFor:
    kind, sep, rest = value.partition(":")
    if kind == "wav" and sep and rest:
        heard = Path(rest)

        def wav(name: str) -> Sink:
            heard.mkdir(parents=True, exist_ok=True)
            return Wav(heard / f"{name}.wav")

        return wav
    if value == "null":
        return lambda name: Null()
    if value == "pc":
        return lambda name: pc.PcSink()
    raise argparse.ArgumentTypeError(f"{value!r} isn't null, wav:DIR or pc")


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / PROG
