"""The HIL tier's hub, as `serve --auto-approve` runs it, on the LAN, with the Echo connected to
it, and an agent's Session on it: it notes each notification as it comes in, on the system
clock, which the hub's `t` is on too."""

import asyncio
import json
import math
import random
import struct
import subprocess
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import tinyaisense
from tinyaisense_hub.bench.corpus import BenchError
from tinyaisense_hub.bench.hub import HubProcess
from tinyaisense_hub.config import Config
from tinyaisense_wire import JsonObject

from .checks import Edge
from .console import Console

RATE = 16000
ONLINE_S = 30
"""How long the Echo may take to come online: its backoff is 5 s at most."""
EDGE_S = 1.0
"""How long an edge may take to reach the agent before it counts as lost."""
STALE_MS = 20
"""How much earlier than its `btn` went out an edge may be on the hub's clock and still be
that `btn`'s: the edge before it of the same kind is a gap and a hold earlier."""
GAP_MS = (30.0, 120.0)
HOLD_MS = (20.0, 60.0)
"""How long before each press, and how long each is held: at random, within."""


def now_ms() -> float:
    """Epoch ms, as `Edge` has them."""
    return time.time_ns() / 1e6


def tone(ms: int) -> bytes:
    """`ms` of a quiet 1 kHz sine, s16le at `RATE`: the Echo's speaker barely plays under
    500 Hz."""
    n, step = RATE * ms // 1000, 2 * math.pi * 1000 / RATE
    return struct.pack(f"<{n}h", *(round(32767 * 0.05 * math.sin(step * i)) for i in range(n)))


def route(ip: str) -> tuple[str, str]:
    """The interface the route to `ip` goes out of, and its source address. Raises `OSError`
    or `subprocess.CalledProcessError`."""
    out = subprocess.run(
        ["ip", "-j", "route", "get", ip], capture_output=True, text=True, check=True
    ).stdout
    hop = cast(JsonObject, json.loads(out)[0])
    return cast(str, hop["dev"]), cast(str, hop["prefsrc"])


@asynccontextmanager
async def hil_hub(
    device_id: str, host: str, ports: tuple[int, int], state: Path | None
) -> AsyncGenerator[HilHub]:
    """A hub on `host`, listening on `ports` for devices and HTTP, from a copy of the `state`
    the Echo `device_id` is paired in, with no speech engines; and a Session on it once the
    Echo is online. Raises `BenchError` with the end of the hub's log."""
    config = Config(agent_token="")
    async with (
        HubProcess(config, host=host, ports=ports, state=state) as process,
        tinyaisense.connect(process.agent_url, process.token) as session,
    ):
        hub = HilHub(process, session, device_id)
        try:
            for device in await session.devices():
                manifest = cast(JsonObject, device["manifest"])
                if manifest["device_id"] == device_id and device["online"]:
                    hub.device = cast(str, device["name"])
                    hub.online.set()
            try:
                await asyncio.wait_for(hub.online.wait(), ONLINE_S)
            except TimeoutError:
                raise BenchError(
                    f"the Echo didn't come online within {ONLINE_S} s: is it paired in "
                    f"{state}, the rig's `state`?"
                ) from None
            yield hub
        finally:
            await hub.close()


class HilHub:
    """The hub `process`, and the agent's Session on it: it notes the raw button edges of the
    Echo `device_id`, its `device.offline`s, and each stream's end."""

    def __init__(self, process: HubProcess, session: tinyaisense.Session, device_id: str) -> None:
        self.process = process
        self.session = session
        self._device_id = device_id
        self.device = ""
        """The Echo's name on the hub."""
        self.online = asyncio.Event()
        self.events: list[str] = []
        """The raw button events, `down` or `up`, in order."""
        self.offline = 0
        self._edges: list[tuple[str, int, float]] = []
        """Each raw button edge, `down` or `up`, its `t`, and when the agent got it."""
        self._taken = 0
        self._ended: dict[int, JsonObject] = {}
        self._changed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)

    def forget(self) -> None:
        """Start the events and the `device.offline`s over."""
        self.events.clear()
        self.offline = 0

    async def presses(self, console: Console, count: int, rng: random.Random) -> list[Edge]:
        """Press `count` times on `console`, each edge once the one before has reached the
        agent, or counts as lost."""
        edges: list[Edge] = []
        for _ in range(count):
            await asyncio.sleep(rng.uniform(*GAP_MS) / 1000)
            edges.append(await self.got("down", console.press("down")))
            await asyncio.sleep(rng.uniform(*HOLD_MS) / 1000)
            edges.append(await self.got("up", console.press("up")))
        return edges

    async def got(self, edge: Literal["down", "up"], sent: float, within: float = EDGE_S) -> Edge:
        """The `edge` whose `btn` went out at `sent`, once it reaches the agent, or lost if it
        doesn't `within` s. Edges before it it hasn't taken yet are passed over."""
        try:
            async with asyncio.timeout(within):
                while True:
                    while self._taken < len(self._edges):
                        which, t, got = self._edges[self._taken]
                        self._taken += 1
                        if which == edge and t >= sent - STALE_MS:
                            return Edge(edge, sent, t, got)
                    await self._change()
        except TimeoutError:
            return Edge(edge, sent, None, None)

    async def play(self, ms: int) -> JsonObject:
        """Play `ms` of a tone through, and return its stream's end."""
        handle = await self.start_tone(ms)
        return await self.session.stream_drain(handle)

    async def start_tone(self, ms: int) -> int:
        """Start `ms` of a tone on the speaker, all sent at once, and return its handle."""
        handle = await self.session.stream_start(f"{self.device}/speaker")
        await self.session.send_audio(handle, tone(ms))
        return handle

    async def record(self, ms: int) -> JsonObject:
        """Run a mic stream for `ms`, reading its frames, and return its end."""
        handle = await self.session.stream_start(f"{self.device}/mic")

        async def read() -> None:
            async for _ in self.session.audio(handle):
                pass

        reader = asyncio.create_task(read())
        try:
            await asyncio.sleep(ms / 1000)
            return await self.session.stream_stop(handle)
        finally:
            await asyncio.wait([reader], timeout=2)
            reader.cancel()

    async def ended(self, handle: int, within: float) -> JsonObject:
        """The stream's end, `{reason, stats}`, from its `stream.ended`. Raises `TimeoutError`
        if it hasn't ended `within` s."""
        async with asyncio.timeout(within):
            while (end := self._ended.get(handle)) is None:
                await self._change()
            return end

    async def back_online(self, within: float) -> None:
        """Wait until the Echo has gone offline and come back. Raises `TimeoutError`."""
        async with asyncio.timeout(within):
            while not self.offline or not self.online.is_set():
                await self._change()

    async def diag(self) -> JsonObject:
        async with self.process.owner() as owner:
            return await owner.diag(self.device)

    async def _change(self) -> None:
        self._changed.clear()
        await self._changed.wait()

    async def _read(self) -> None:
        async for notification in self.session.notifications():
            got, params = now_ms(), notification.params
            match notification.method:
                case "button.down" | "button.up":
                    edge = notification.method.removeprefix("button.")
                    self.events.append(edge)
                    self._edges.append((edge, notification.t, got))
                case "device.online" if (
                    cast(JsonObject, params["manifest"])["device_id"] == self._device_id
                ):
                    self.device = cast(str, params["device"])
                    self.online.set()
                case "device.offline" if params["device"] == self.device:
                    self.offline += 1
                    self.online.clear()
                case "stream.ended":
                    self._ended[cast(int, params["handle"])] = {
                        "reason": params["reason"],
                        "stats": params["stats"],
                    }
                case _:
                    pass
            self._changed.set()
