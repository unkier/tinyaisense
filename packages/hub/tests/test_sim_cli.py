"""`tinyaisense-sim`, run under a pty against an in-process hub that pairs it by itself: keys
in, the agent's view and the screen out."""

import asyncio
import contextlib
import json
import os
import pty
import re
import sys
import wave
from collections.abc import AsyncGenerator
from pathlib import Path

import tinyaisense
from tinyaisense_hub import Hub
from tinyaisense_sim import device_id_for, sources

from .conftest import StartHub
from .test_faults import TOKEN, next_of
from .test_profiles import THERMO


class Terminal:
    """`tinyaisense-sim` on a pty: what it has written so far, and keys to type."""

    def __init__(self, process: asyncio.subprocess.Process, master: int) -> None:
        self.process = process
        self.screen = ""
        """Everything written so far, escapes and all."""
        self._master = master
        self._seen = 0
        self._written = asyncio.Event()
        asyncio.get_running_loop().add_reader(master, self._read)

    def type(self, keys: str) -> None:
        os.write(self._master, keys.encode())

    async def wait_for(self, pattern: str) -> re.Match[str]:
        """The first match of `pattern` in what's written past the last match, as `expect`
        does."""
        async with asyncio.timeout(5):
            while (match := re.compile(pattern).search(self.screen, self._seen)) is None:
                await self._written.wait()
                self._written.clear()
        self._seen = match.end()
        return match

    def close(self) -> None:
        asyncio.get_running_loop().remove_reader(self._master)
        os.close(self._master)

    def _read(self) -> None:
        try:
            self.screen += os.read(self._master, 65536).decode(errors="replace")
        except OSError:  # the process is gone
            asyncio.get_running_loop().remove_reader(self._master)
        self._written.set()


@contextlib.asynccontextmanager
async def tinyaisense_sim(hub: Hub, tmp_path: Path, *args: str) -> AsyncGenerator[Terminal]:
    """`tinyaisense-sim` with `args`, pointed at `hub`."""
    host, port = hub.device_address
    master, slave = pty.openpty()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tinyaisense_sim",
        "--hub",
        f"{host}:{port}",
        "--state-dir",
        str(tmp_path / "sim"),
        *args,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    terminal = Terminal(process, master)
    try:
        yield terminal
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()
        terminal.close()


async def test_the_keys_work_the_selected_devices_button_wifi_and_connection(
    start_hub: StartHub, tmp_path: Path
) -> None:
    hub = await start_hub(auto_approve=True)
    async with (
        tinyaisense_sim(hub, tmp_path, "--device", "m5stack-atom-echo:kitchen") as terminal,
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
    ):
        notifications = agent.notifications()
        await terminal.wait_for("kitchen.*online")

        terminal.type(" ")
        await next_of(notifications, "button.down")
        terminal.type(" ")
        await next_of(notifications, "button.up")
        terminal.type("c")
        await next_of(notifications, "button.down")
        await next_of(notifications, "button.up")
        terminal.type("d")
        for _ in range(2):
            await next_of(notifications, "button.down")
            await next_of(notifications, "button.up")

        terminal.type("w")
        await next_of(notifications, "device.offline")
        terminal.type("w")
        await next_of(notifications, "device.online")
        terminal.type("r")
        await next_of(notifications, "device.offline")
        await next_of(notifications, "device.online")

        terminal.type("q")
        assert await asyncio.wait_for(terminal.process.wait(), 5) == 0


async def test_each_device_has_a_status_line_with_its_leds_and_tab_selects_the_next(
    start_hub: StartHub, tmp_path: Path
) -> None:
    hub = await start_hub(auto_approve=True)
    second = f"echo-{device_id_for('sim-2')[-4:]}"  # as the hub names it
    async with (
        tinyaisense_sim(hub, tmp_path, "--count", "2") as terminal,
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
    ):
        await terminal.wait_for(r"> sim-1 .*online")
        await terminal.wait_for(r"  sim-2 .*online")

        await agent.command(f"{second}/led", "set", {"color": "#00ff00"})
        # Green, at the Echo's brightness cap of a quarter: half as bright to the eye.
        await terminal.wait_for(r"  sim-2 +\x1b\[38;2;0;128;0m██\x1b\[0m online +claim")

        terminal.type("\t ")
        await terminal.wait_for(r"  sim-1 .*\n> sim-2 .*button down")


async def test_trace_writes_every_devices_timeline_and_vv_logs_it_too(
    start_hub: StartHub, tmp_path: Path
) -> None:
    hub = await start_hub(auto_approve=True)
    trace = tmp_path / "trace.jsonl"
    args = ("--device", "m5stack-atom-echo:kitchen", "--trace", str(trace), "-vv")
    async with tinyaisense_sim(hub, tmp_path, *args) as terminal:
        await terminal.wait_for("kitchen .*online")
        terminal.type(" ")
        await terminal.wait_for(r'kitchen edge \{"cap":"button","edge":"down"\}')
        terminal.type("q")
        assert await asyncio.wait_for(terminal.process.wait(), 5) == 0

    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    edges = [
        (line["device"], line["cap"], line["edge"]) for line in lines if line["type"] == "edge"
    ]
    assert edges == [("kitchen", "button", "down")]
    assert [line["status"] for line in lines if line["type"] == "status"][:2] == [
        "connecting",
        "no_agent",
    ]


async def test_sink_wav_writes_what_each_speaker_plays(start_hub: StartHub, tmp_path: Path) -> None:
    hub = await start_hub(auto_approve=True)
    kitchen = f"echo-{device_id_for('kitchen')[-4:]}"
    args = ("--device", "m5stack-atom-echo:kitchen", "--sink", f"wav:{tmp_path / 'heard'}")
    async with (
        tinyaisense_sim(hub, tmp_path, *args) as terminal,
        tinyaisense.connect(hub.agent_url, TOKEN) as agent,
    ):
        await terminal.wait_for("kitchen .*online")
        handle = await agent.stream_start(f"{kitchen}/speaker")
        await agent.send_audio(handle, sources.tone(300))
        drained = await agent.stream_drain(handle)
        terminal.type("q")
        assert await asyncio.wait_for(terminal.process.wait(), 5) == 0

    assert drained["stats"]["played_ms"] == 300
    with wave.open(str(tmp_path / "heard" / "kitchen.wav")) as heard:
        assert (heard.getframerate(), heard.getnframes()) == (16000, 4800)


async def test_a_manifest_runs_a_device_of_it_under_the_flags_faults(
    start_hub: StartHub, tmp_path: Path
) -> None:
    hub = await start_hub(auto_approve=True)
    manifest, trace = tmp_path / "thermo.json", tmp_path / "trace.jsonl"
    manifest.write_text(json.dumps(THERMO))
    args = ("--manifest", str(manifest), "--drift-ppm", "100", "--trace", str(trace))
    async with tinyaisense_sim(hub, tmp_path, *args) as terminal:
        await terminal.wait_for("sim-1 .*online")
        async with tinyaisense.connect(hub.agent_url, TOKEN) as agent:
            [device] = await agent.devices()
        terminal.type("q")
        assert await asyncio.wait_for(terminal.process.wait(), 5) == 0

    assert device["manifest"]["board"] == "thermo-1"
    faults = [json.loads(line) for line in trace.read_text().splitlines() if '"fault"' in line]
    assert faults == [
        {
            "t": faults[0]["t"],
            "device": "sim-1",
            "type": "fault",
            "fault": "drift_ppm",
            "ppm": 100.0,
        }
    ]
