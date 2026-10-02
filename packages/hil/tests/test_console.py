"""The Echo's serial console, against a fake of the firmware's on a pty: commands run to the
next prompt, whatever the firmware logs meanwhile, and presses go out at once."""

import os
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from tinyaisense_hil.console import Console, ConsoleError, Cut, EchoStatus, parse_status

STATUS = """\
device    f4650b08536c, m5stack-atom-echo, firmware 4766d3c-dirty, elf 2bc1ea9f43e7fce6
wifi      TP-Link, 2.4 GHz, 192.168.0.103, -52 dBm
hub       192.168.0.106:8499, at 192.168.0.106:8499
conn      welcomed as echo-536c, 2 lost
paired    yes
status    agent
volume    80, speaker
reflex    armed, button
udp       port 8420, 10 in, 20 out, 4 mirrors
heap      120000 B free, 90000 B lowest
uptime    1234 s
"""
LOG = "\x1b[0;32mI (1234) ctrl: welcomed as echo-536c\x1b[0m\n"


def test_status_says_who_the_echo_is_and_where_it_is() -> None:
    assert parse_status(STATUS) == EchoStatus(
        device_id="f4650b08536c",
        board="m5stack-atom-echo",
        firmware="4766d3c-dirty",
        elf="2bc1ea9f43e7fce6",
        ip="192.168.0.103",
        hub="192.168.0.106:8499",
        name="echo-536c",
        uptime_s=1234,
    )


def test_status_of_an_echo_offline_with_old_firmware() -> None:
    status = STATUS.replace(", elf 2bc1ea9f43e7fce6", "")
    status = status.replace("TP-Link, 2.4 GHz, 192.168.0.103, -52 dBm", "TP-Link, down")
    status = status.replace("192.168.0.106:8499, at 192.168.0.106:8499", "auto")
    status = status.replace("welcomed as echo-536c, 2 lost", "none, 2 lost")

    parsed = parse_status(status)

    assert parsed.elf is None
    assert parsed.ip is None
    assert parsed.hub == "auto"
    assert parsed.name is None


def test_status_that_isnt_one_is_refused() -> None:
    with pytest.raises(ConsoleError, match="status"):
        parse_status("unknown command; try help\n")


class FakeEcho:
    """The firmware's console on a pty: it echoes what's typed, runs each line, prints a
    prompt, and logs out of turn when told to."""

    def __init__(self, master: int) -> None:
        self._master = master
        self.lines: list[str] = []
        self.answers: dict[str, Callable[[], str]] = {"status": lambda: STATUS}
        self.silent: set[str] = set()
        """Lines it answers with no prompt, as when it reboots."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def log(self, text: str) -> None:
        os.write(self._master, text.encode())

    def _run(self) -> None:
        line = b""
        while True:
            try:
                data = os.read(self._master, 64)
            except OSError:
                return
            for c in data:
                if c == ord("\r"):
                    if not line:
                        continue
                    self.lines.append(line.decode())
                    if line.decode() in self.silent:
                        line = b""
                        continue
                    answer = self.answers.get(line.decode(), lambda: "")()
                    os.write(self._master, b"\n" + answer.replace("\n", "\r\n").encode())
                    os.write(self._master, b"tas> ")
                    line = b""
                elif c != ord("\n"):
                    line += bytes([c])
                    os.write(self._master, bytes([c]))


@pytest.fixture
def echo() -> Iterator[tuple[FakeEcho, Console]]:
    master, slave = os.openpty()
    fake = FakeEcho(master)
    console = Console(os.ttyname(slave))
    yield fake, console
    console.close()
    os.close(slave)
    os.close(master)


def test_a_command_runs_to_the_next_prompt(echo: tuple[FakeEcho, Console]) -> None:
    fake, console = echo
    fake.answers["volume"] = lambda: "volume set\n"

    assert console.run("volume") == "volume set"
    assert fake.lines == ["volume"]


def test_status_skips_what_the_firmware_logs_meanwhile(echo: tuple[FakeEcho, Console]) -> None:
    fake, console = echo
    lines = STATUS.splitlines(keepends=True)
    fake.answers["status"] = lambda: LOG + "".join(lines[:4]) + LOG + "".join(lines[4:])
    fake.log(LOG)

    assert console.status().name == "echo-536c"


def test_a_press_goes_out_at_once_and_the_next_command_waits_behind_it(
    echo: tuple[FakeEcho, Console],
) -> None:
    fake, console = echo
    fake.answers["btn down"] = lambda: ""

    before = time.time() * 1000
    at = console.press("down")

    assert before <= at <= time.time() * 1000
    assert console.status().device_id == "f4650b08536c"
    assert fake.lines == ["btn down", "status"]


def test_a_command_with_no_prompt_back_times_out_and_the_next_runs(
    echo: tuple[FakeEcho, Console],
) -> None:
    fake, console = echo
    fake.silent.add("reboot")

    with pytest.raises(ConsoleError, match="no prompt"):
        console.run("reboot", timeout=0.2)
    assert console.status().uptime_s == 1234


def test_the_speakers_cuts_are_taken_from_its_log_in_order(
    echo: tuple[FakeEcho, Console],
) -> None:
    fake, console = echo
    fake.log("\x1b[0;32mI (5000) speaker: speaker cut: silent after 4460 us, from the pr")
    fake.log("ess\x1b[0m\r\n" + LOG)
    fake.log("I (6000) speaker: speaker cut: silent after 812 us\r\n")

    assert console.cut(0, timeout=1) == Cut("speaker", 4460, by_reflex=True)
    assert console.cut(1, timeout=1) == Cut("speaker", 812, by_reflex=False)
    assert console.cut(2, timeout=0.1) is None


def test_a_cut_logged_while_a_command_runs_is_taken_too(echo: tuple[FakeEcho, Console]) -> None:
    fake, console = echo
    fake.answers["cut"] = lambda: "I (7000) speaker: speaker cut: silent after 900 us\n"
    taken = console.cuts

    console.run("cut")

    assert console.cut(taken, timeout=1) == Cut("speaker", 900, by_reflex=False)
