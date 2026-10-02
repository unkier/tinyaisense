"""The rig, set up once a run: the Echo on the debug build, flashed only if the build changed,
online, and pointed at where the HIL hub listens; and the acceptance report the tests fill in,
written at the end.

Env: `TINYAISENSE_HIL_RIG` names the rig config (see `tinyaisense_hil.rig`),
`TINYAISENSE_HIL_OUT` where the report goes (default `acceptance/`), and
`TINYAISENSE_HIL_SOAK_MINUTES` how long the soak runs (default: the rig's `soak_minutes`)."""

import contextlib
import os
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tinyaisense_hil import firmware
from tinyaisense_hil.console import Console, ConsoleError, EchoStatus
from tinyaisense_hil.hub import HilHub, hil_hub, route
from tinyaisense_hil.report import HilReport
from tinyaisense_hil.rig import Rig, RigError, find_rig, load_rig
from tinyaisense_hub.bench.hub import free_ports, hub_version
from tinyaisense_hub.bench.report import machine
from tinyaisense_hub.config import SpeechConfig, state_dir

BOOT_S = 30
"""How long the Echo may take to answer on its console, and to join the WiFi."""
TIMEOUT_S = 1200
"""How long a test may take, besides the soak, with the build and flash it may set up first."""
_summary: list[str] = []
"""What the run's summary says: the machine's load and governor, and the report's paths."""
_broke: list[str] = []
"""What broke off each test that didn't get to its asserts."""


@dataclass
class Hil:
    """The rig, set up."""

    console: Console
    status: EchoStatus
    """The Echo's, once it's online."""
    host: str
    """The hub's address, on the route to the Echo."""
    ports: tuple[int, int]
    state: Path | None
    soak_minutes: float
    firmware: dict[str, object] = field(default_factory=dict[str, object])
    rig: dict[str, object] = field(default_factory=dict[str, object])

    def hub(
        self, speech: SpeechConfig | None = None
    ) -> contextlib.AbstractAsyncContextManager[HilHub]:
        """A HIL hub, with the `speech` engines, none by default, and a Session on it once the
        Echo is online."""
        return hil_hub(self.status.device_id, self.host, self.ports, self.state, speech)


@pytest.fixture(scope="session")
def rig() -> Rig:
    try:
        return load_rig(find_rig())
    except RigError as e:
        pytest.fail(str(e), pytrace=False)


@pytest.fixture(scope="session")
def hil(rig: Rig, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Hil]:
    with contextlib.ExitStack() as stack:
        serial_log = stack.enter_context((tmp_path_factory.mktemp("hil") / "serial.log").open("wb"))
        console = Console(rig.port, serial_log)
        stack.callback(lambda: console.close())  # whichever is open by then
        built = firmware.build(rig.pio)
        try:
            flashed = firmware.needs_flash(_answering(console), built)
        except ConsoleError:
            flashed = True  # e.g. a flash cut short
        if flashed:
            console.close()
            firmware.flash(rig.pio, rig.port)
            console = Console(rig.port, serial_log)
        status = _online(console)
        if firmware.needs_flash(status, built):
            pytest.fail(f"the Echo runs elf {status.elf}, not the build's {built[:16]}")
        assert status.ip is not None
        echo_ip = rig.echo_ip or status.ip
        iface, hub_ip = route(echo_ip)
        hub_ip = rig.hub_ip or hub_ip
        ports = free_ports(hub_ip)
        state = Path(rig.state) if rig.state else state_dir() / "state.json"
        console.run(f"hub {hub_ip}:{ports[0]}")
        stack.callback(console.run, f"hub {status.hub}")
        env = stack.enter_context(pytest.MonkeyPatch.context())
        for name in ("NO_PROXY", "no_proxy"):  # the agent's link: a proxy can't reach the hub
            env.setenv(name, ",".join(filter(None, [os.environ.get(name), hub_ip])))
        yield Hil(
            console,
            status,
            hub_ip,
            ports,
            state if state.exists() else None,
            float(os.environ.get("TINYAISENSE_HIL_SOAK_MINUTES") or rig.soak_minutes),
            firmware={"version": status.firmware, "elf": status.elf, "flashed": flashed},
            rig={
                "port": rig.port,
                "device_id": status.device_id,
                "board": status.board,
                "echo_ip": echo_ip,
                "iface": iface,
                "hub_ip": hub_ip,
                "ports": list(ports),
                "state": str(state),
                "pio": rig.pio,
                "serial_log": serial_log.name,
                "pod": rig.pod,
                "mouth": rig.mouth,
            },
        )


@pytest.fixture(scope="session")
def report(hil: Hil) -> Iterator[HilReport]:
    """The acceptance report, which each test adds its part to: written once they've run."""
    report = HilReport(
        date=datetime.now(UTC),
        hub=hub_version(),
        firmware=hil.firmware,
        rig=hil.rig,
        machine=machine(),
    )
    yield report
    report.errors += _broke
    report.loadavg_after = after = os.getloadavg()
    out = os.environ.get("TINYAISENSE_HIL_OUT") or firmware.FIRMWARE.parent / "acceptance"
    _summary.append(
        f"HIL: governor {report.machine['governor']}, loadavg {report.machine['loadavg']} at "
        f"the start, {[round(load, 2) for load in after]} at the end"
    )
    _summary.extend(f"HIL report: {path}" for path in report.write(Path(out)))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    outcome = yield
    if call.when == "call" and call.excinfo and not call.excinfo.errisinstance(AssertionError):
        what = (str(call.excinfo.value).splitlines() or [""])[0][:300]
        _broke.append(f"{item.name}: {call.excinfo.typename}: {what}")
    return outcome


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for line in _summary:
        terminalreporter.write_line(line)


def _answering(console: Console) -> EchoStatus:
    """The Echo's status, once its console answers."""
    deadline = time.monotonic() + BOOT_S
    while True:
        try:
            return console.status()
        except ConsoleError:
            if time.monotonic() > deadline:
                raise


def _online(console: Console) -> EchoStatus:
    """The Echo's status, once it's on the WiFi."""
    deadline = time.monotonic() + BOOT_S
    while (status := _answering(console)).ip is None:
        if time.monotonic() > deadline:
            pytest.fail(f"the Echo didn't join the WiFi within {BOOT_S} s")
        time.sleep(1)
    return status
