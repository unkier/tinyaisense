"""What an MCP-only Session hears: its Event queue, and the Utterances its `say`s return.

MCP has no way to push notifications, so the Session's notifications land here. Those an
agent must not miss are queued as Events, which ride on its next tool result. What the human
says in answer to a `say`, with `then_listen` or cutting it off with the button, comes back
with the `say` instead.
"""

import asyncio
import time
from collections import deque

from tinyaisense_wire import JsonObject

from .session import EndReason

QUEUE = 100
"""How many Events wait at most: past it, the oldest is dropped."""
CUT_UTTERANCE_MS = 1000
"""How soon after the Reflex cuts a `say` the Utterance its press opens must start, for the
`say` to wait for it."""


class _Cut:
    """The Reflex's cut of the `say`s on a device, and what the human said in the Utterance
    its press opened: None if none did."""

    def __init__(self) -> None:
        self.said: asyncio.Future[JsonObject | None] = asyncio.get_running_loop().create_future()
        self.opened = False
        """Whether the Utterance the press opened has started."""
        self.waiters: list[asyncio.Event] = []
        """Of each `say` that waits for it, what stops it."""
        self.expiry: asyncio.TimerHandle | None = None

    @property
    def awaited(self) -> bool:
        """Whether a `say` waits for it, that isn't stopped."""
        return any(not stop.is_set() for stop in self.waiters)

    def end(self, said: JsonObject | None) -> None:
        if self.expiry is not None:
            self.expiry.cancel()
        if not self.said.done():
            self.said.set_result(said)


class Inbox:
    """An MCP-only Session's sink: its Event queue."""

    def __init__(self) -> None:
        self._events: deque[JsonObject] = deque(maxlen=QUEUE)
        self._dropped = 0
        """Events dropped from the queue since the last delivery."""
        self._queued = asyncio.Event()
        """Set while an Event waits, or once the Session has ended."""
        self._ended = False
        self._last_pending = 0
        self._utterances: dict[int, JsonObject] = {}
        """The `utterance.start` params of each Utterance under way, by id."""
        self._answers: dict[int, asyncio.Future[JsonObject]] = {}
        """What the human said in each Utterance a `then_listen` opened, by id, until its
        `say` takes it."""
        self._cuts: dict[str, _Cut] = {}
        """Device name → the Reflex's last cut of its `say`s, until the Utterance it opened
        ends."""
        self._opened: dict[int, _Cut] = {}
        """Utterance id → the cut whose press opened it."""

    def take(self) -> JsonObject:
        """The Events queued, under `events`, each once, and `dropped`: how many were dropped
        for want of room since the last delivery. Either is left out if there are none."""
        taken: JsonObject = {}
        if self._events:
            taken["events"] = list(self._events)
            self._events.clear()
        if self._dropped:
            taken["dropped"] = self._dropped
            self._dropped = 0
        if not self._ended:
            self._queued.clear()
        return taken

    async def wait(self) -> None:
        """Wait until an Event is queued, or the Session ends."""
        await self._queued.wait()

    async def answer(self, utterance_id: int) -> JsonObject:
        """What the human said in the Utterance `utterance_id` that a `then_listen` opened,
        once it ends."""
        try:
            return await asyncio.shield(self._answer(utterance_id))
        finally:
            self._answers.pop(utterance_id, None)

    async def cut(self, device: str, stop: asyncio.Event) -> JsonObject | None:
        """What the human said in the Utterance that the press which cut the `say`s on
        `device` opened, once it ends; None if none started soon after the cut, or if `stop`
        is set first: then it's queued."""
        cut = self._cuts.get(device)
        if cut is None:
            return None
        cut.waiters.append(stop)
        stopping = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait((cut.said, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            cut.waiters.remove(stop)
        return cut.said.result() if cut.said.done() else None

    def pending(self) -> int:
        """A new `pending` id: of a call whose result is to come as an Event."""
        self._last_pending += 1
        return self._last_pending

    def late(self, pending: int, tool: str, result: JsonObject, error: bool) -> None:
        """Queue the result of the call of `tool` that returned `pending`, or its error."""
        key = "error" if error else "result"
        self._queue(
            {"event": "result", "pending": pending, "tool": tool, key: result, "t": _now_ms()}
        )

    # The Session's sink.

    def notify(
        self,
        method: str,
        params: JsonObject,
        after: int | None = None,
        latency_from: int | None = None,
    ) -> None:
        """Queue what's to be queued, and follow the Utterances."""
        match method:
            case "device.online" | "device.offline":
                device: str = params["device"]
                self._queue({"event": method, "device": device, "t": params["t"]})
                cut = self._cuts.get(device)
                if method == "device.offline" and cut is not None and not cut.opened:
                    self._expire(device, cut)  # no Utterance starts on it now
            case "button.click" | "button.double_click" | "button.long_press":
                self._queue({"event": method, "address": params["address"], "t": params["t"]})
            case "say.ended" if params["reason"] == "reflex":
                self._cut(_device(params["address"]))
            case "utterance.start":
                self._utterance_start(params)
            case "utterance.end":
                self._utterance_end(params)
            case _:
                pass  # partials, raw button edges, streams: not for MCP

    def audio(self, handle: int, index: int, pcm: bytes) -> None:
        pass  # MCP takes no audio

    def audio_ended(self, handle: int) -> int:
        return 0

    def ended(self, reason: EndReason) -> None:
        """Its Utterances were delivered first: any a `say` still waits for never started."""
        self._ended = True
        self._queued.set()
        for cut in [*self._cuts.values(), *self._opened.values()]:
            cut.end(None)
        for answer in self._answers.values():
            if not answer.done():
                answer.set_result({"text": "", "reason": "disconnected"})

    def _cut(self, device: str) -> None:
        """The Reflex cut a `say` on `device`: the Utterance its press opens is the `say`'s
        to return, if it starts soon."""
        if (last := self._cuts.get(device)) is not None and not last.opened:
            return  # the say after it, cut by the same press
        cut = self._cuts[device] = _Cut()
        cut.expiry = asyncio.get_running_loop().call_later(
            CUT_UTTERANCE_MS / 1000, self._expire, device, cut
        )

    def _expire(self, device: str, cut: _Cut) -> None:
        """No Utterance started for the cut."""
        if self._cuts.get(device) is cut:
            del self._cuts[device]
        cut.end(None)

    def _utterance_start(self, params: JsonObject) -> None:
        utterance_id: int = params["utterance_id"]
        self._utterances[utterance_id] = params
        cut = self._cuts.get(_device(params["address"]))
        if params["trigger"] == "button" and cut is not None and not cut.opened:
            cut.opened = True
            self._opened[utterance_id] = cut
            if cut.expiry is not None:
                cut.expiry.cancel()

    def _utterance_end(self, params: JsonObject) -> None:
        """Hand what the human said to the `say` that waits for it; or queue it, if it's
        push-to-talk's final transcript. A click's has nothing heard: its gesture says it."""
        utterance_id: int = params["utterance_id"]
        start = self._utterances.pop(utterance_id, None)
        said = _said(params)
        if start is None or start["trigger"] == "then_listen":  # None: its mic didn't start
            if not (answer := self._answer(utterance_id)).done():
                answer.set_result(said)
            return
        if (cut := self._opened.pop(utterance_id, None)) is not None:
            device = _device(start["address"])
            if self._cuts.get(device) is cut:
                del self._cuts[device]
            cut.end(said)
            if cut.awaited:
                return
        if start["trigger"] == "button" and params["reason"] != "click":
            event = {"event": "utterance", "address": start["address"]} | said
            self._queue(event | {"t": params["t"]})

    def _answer(self, utterance_id: int) -> asyncio.Future[JsonObject]:
        answer = self._answers.get(utterance_id)
        if answer is None:
            answer = self._answers[utterance_id] = asyncio.get_running_loop().create_future()
        return answer

    def _queue(self, event: JsonObject) -> None:
        if len(self._events) == QUEUE:
            self._dropped += 1
        self._events.append(event)
        self._queued.set()


def _said(end: JsonObject) -> JsonObject:
    """What the human said, from an `utterance.end`'s params: `text`, its `lang`, and the
    `reason` it ended."""
    utterance: JsonObject = {"text": end.get("transcript", "")}
    if "lang" in end:
        utterance["lang"] = end["lang"]
    return utterance | {"reason": end["reason"]}


def _device(address: str) -> str:
    """The device an Address names."""
    return address.partition("/")[0]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
