"""Cloud fallback (ADR 0004): a cloud engine, with a local one that stands in when it fails.

A cloud engine fails when it raises, or gives nothing within `cloud_first_byte_ms`. After
`FAILURES` in a row, it's skipped for `cloud_skip_ms`, so that an outage doesn't cost the wait
on every call. Once that's over it's tried again, and one more failure skips it again. It runs
on threads of its own, so that the stand-in never waits behind it, and nor does its next call
behind a reply it gave up waiting for.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..alarm import now_us
from ..config import Timing
from .engines import Engine

FAILURES = 3
"""In a row, from which the cloud engine is skipped."""
THREADS = 4
"""A cloud engine runs on this many."""

_log = logging.getLogger("tinyaisense_hub.speech")


class Breaker:
    """Whether to try the cloud engine. Sans-IO: times are µs on the hub's monotonic clock."""

    def __init__(self, *, skip_ms: int) -> None:
        self._skip = skip_ms * 1000
        self._failures = 0
        self._until = 0
        """When the skip ends."""

    def closed(self, now: int) -> bool:
        """Whether to try the cloud engine now."""
        return now >= self._until

    def failed(self, now: int) -> None:
        """The cloud engine failed, `now`."""
        self._failures += 1
        if self._failures >= FAILURES:
            self._until = now + self._skip

    def succeeded(self) -> None:
        """The cloud engine answered in time."""
        self._failures = 0


@dataclass(frozen=True, slots=True)
class Runner[E: Engine]:
    """An engine, by its name in the config, and the threads it runs on."""

    name: str
    engine: E
    thread: ThreadPoolExecutor

    async def run[T](self, call: Callable[[E], T]) -> T:
        """`call` the engine, on its thread."""
        return await asyncio.get_running_loop().run_in_executor(self.thread, call, self.engine)


class Engines[E: Engine]:
    """What serves a role in a language: an engine; or a cloud engine, and the local engine
    that stands in when it fails."""

    def __init__(self, engine: Runner[E], stand_in: Runner[E] | None, timing: Timing) -> None:
        self.engine = engine
        self.stand_in = stand_in
        self._first_byte = timing.cloud_first_byte_ms / 1000
        self._breaker = Breaker(skip_ms=timing.cloud_skip_ms)

    @property
    def runners(self) -> tuple[Runner[E], ...]:
        """The engine, and its stand-in if it has one."""
        return (self.engine,) if self.stand_in is None else (self.engine, self.stand_in)

    async def call[T](self, call: Callable[[Runner[E], bool], Awaitable[T]]) -> tuple[T, bool]:
        """`call(engine, False)`; or, if that's a cloud engine that fails or is skipped,
        `call(stand_in, True)`. `call` should return once the engine has given its first
        byte. Returns what it returned, and whether the stand-in stood in."""
        stand_in = self.stand_in
        if stand_in is None:
            return await call(self.engine, False), False
        if self._breaker.closed(now_us()):
            try:
                result = await asyncio.wait_for(call(self.engine, False), self._first_byte)
            except Exception as e:
                self._breaker.failed(now_us())
                why = "gave nothing in time" if isinstance(e, TimeoutError) else f"failed: {e}"
                skipped = "" if self._breaker.closed(now_us()) else ", and is skipped for a while"
                _log.warning(
                    "speech engine %r %s: %r stands in%s",
                    self.engine.name,
                    why,
                    stand_in.name,
                    skipped,
                )
            else:
                self._breaker.succeeded()
                return result, False
        return await call(stand_in, True), True
