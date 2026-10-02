"""The Echo's serial console (firmware/components/tas_core/console.c), and the debug build's test
hooks on it: `btn down|up` injects a press as the button's interrupt would see it.

It's opened with DTR and RTS low, so that opening it doesn't reset the Echo. A thread reads all
the Echo prints; a command's answer is what follows its echo, up to the next prompt, without the
lines the firmware logs meanwhile."""

import re
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Literal

import serial

PROMPT = "tas> "
BAUD = 115200
_LOGGED = re.compile(r"^[EWIDV] \(\d+\) ")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_WELCOMED = re.compile(r"welcomed as (\S+),")
_KEEP = 64 * 1024
"""How much of what the Echo printed to keep while no command waits for its answer: none of
it is needed then, but it goes only past this, so that it isn't copied each time."""


class ConsoleError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class EchoStatus:
    """What the console's `status` says."""

    device_id: str
    board: str
    firmware: str
    elf: str | None
    """The start of the running app's ELF sha256, in hex; None from firmware that doesn't say."""
    ip: str | None
    """None while its WiFi is down."""
    hub: str
    """Where it looks for the hub: `host:port`, or `auto` for mDNS."""
    name: str | None
    """Its name, while a hub has welcomed it."""
    uptime_s: int


def parse_status(text: str) -> EchoStatus:
    """Raises `ConsoleError` if `text` isn't a `status`."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        fields[key] = value.strip()
    try:
        device_id, board, firmware, *elf = fields["device"].split(", ")
        wifi, hub, conn = fields["wifi"], fields["hub"], fields["conn"]
        uptime_s = int(fields["uptime"].split()[0])
    except KeyError, ValueError, IndexError:
        raise ConsoleError(f"not a status: {text!r}") from None
    welcomed = _WELCOMED.match(conn)
    return EchoStatus(
        device_id=device_id,
        board=board,
        firmware=firmware.removeprefix("firmware "),
        elf=elf[0].removeprefix("elf ") if elf else None,
        ip=wifi.rsplit(", ", 2)[1] if wifi.endswith(" dBm") else None,
        hub=hub.partition(", at ")[0],
        name=welcomed.group(1) if welcomed else None,
        uptime_s=uptime_s,
    )


class Console:
    """The console on serial `port`; `log` gets all the Echo prints. Close it to let go of the
    port, e.g. to flash."""

    def __init__(self, port: str, log: BinaryIO | None = None) -> None:
        self._serial = serial.Serial()
        self._serial.port = port
        self._serial.baudrate = BAUD
        self._serial.timeout = 0.1
        self._serial.dtr = False
        self._serial.rts = False
        self._serial.open()
        self._log = log
        self._text = ""
        self._changed = threading.Condition()
        self._running = threading.Lock()
        self._writing = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read, name=f"console {port}", daemon=True)
        self._reader.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._serial.cancel_read()
        self._reader.join()
        self._serial.close()

    def run(self, line: str, timeout: float = 2.0) -> str:
        """Run `line` and return its answer. Raises `ConsoleError` with no prompt back within
        `timeout` s."""
        with self._running:
            with self._changed:
                start = len(self._text)
            self._write(line)
            with self._changed:
                answer = self._changed.wait_for(lambda: self._answer(start, line), timeout)
                if answer is None:
                    raise ConsoleError(
                        f"no prompt after {line!r} within {timeout} s; the Echo printed "
                        f"{self._text[start:][-500:]!r}"
                    )
                self._text = self._text[answer[1] :]
                return answer[0]

    def status(self) -> EchoStatus:
        return parse_status(self.run("status"))

    def press(self, edge: Literal["down", "up"]) -> float:
        """Inject a button edge, at once: it doesn't wait for the prompt. Returns when it went
        out, just before, in epoch ms."""
        at = time.time_ns() / 1e6
        self._write(f"btn {edge}")
        return at

    def hard_reset(self) -> None:
        """Reset the Echo through its EN pin, as esptool does: a power-on reset to the
        firmware."""
        self._serial.rts = True
        time.sleep(0.1)
        self._serial.rts = False

    def _write(self, line: str) -> None:
        with self._writing:
            self._serial.write(line.encode() + b"\r")

    def _answer(self, start: int, line: str) -> tuple[str, int] | None:
        """What followed `line`'s echo since `start`, up to the next prompt, and where that
        prompt ends; None until it's there."""
        text = self._text
        echo = text.find(line + "\r\n", start)
        if echo < 0:
            echo = text.find(line + "\n", start)
        if echo < 0:
            return None
        begin = text.index("\n", echo) + 1
        end = text.find(PROMPT, begin)
        if end < 0:
            return None
        lines = (_ANSI.sub("", raw).rstrip() for raw in text[begin:end].splitlines())
        return "\n".join(ln for ln in lines if ln and not _LOGGED.match(ln)), end + len(PROMPT)

    def _read(self) -> None:
        while not self._closed:
            try:
                data = self._serial.read(max(1, self._serial.in_waiting))
            except OSError, TypeError:  # closed under it
                return
            if not data:
                continue
            if self._log is not None:
                self._log.write(data)
            with self._changed:
                self._text += data.decode(errors="replace")
                if len(self._text) > _KEEP and not self._running.locked():
                    self._text = ""
                self._changed.notify_all()
