"""`say`: text spoken on a speaker, on a stream the hub runs. The `say`s queued on a speaker
share one stream, back to back."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from tinyaisense_wire import Capability, JsonObject

from ..alarm import now_us
from ..config import SpeechConfig
from ..control import Control
from ..errors import HubError
from ..resampler import Resampler
from ..session import Session
from ..speaker import SpeakerStream
from ..state import DeviceRecord
from ..streams import FORMAT, EndReason, StreamEnd
from .engines import Tts
from .text import Clause, Script

BYTES_PER_MS = FORMAT.frame_bytes // FORMAT.frame_ms
"""Of the device's audio."""

_log = logging.getLogger("tinyaisense_hub.speech")


@dataclass(frozen=True, slots=True)
class EngineVoice:
    """A Voice, as its engine knows it."""

    engine: Tts
    name: str


@dataclass(eq=False)
class Say:
    """One `say`, in a run. Its text may still be coming in, from `say.append`."""

    script: Script
    session: Session
    address: str
    voice: EngineVoice | None = None
    """What it speaks in. Picked by the time its script has a clause."""
    more: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when its script has more clauses, or closes."""
    idle: asyncio.TimerHandle | None = None
    """Cuts the tail of its script once no text has come for a while."""
    done: asyncio.Future[JsonObject] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    """Its result, once it has played or been cut. Raises `HubError` if its stream failed
    to start."""
    start: int | None = None
    """Where its audio starts on the stream, in bytes at the device's rate, once it does."""
    ends: list[int] = field(default_factory=list[int])
    """Where each clause's audio ends on the stream, as it's fed."""
    end: int | None = None
    """Where its audio ends on the stream, once it's all fed, or the TTS failed on it."""
    then: Callable[[], JsonObject] | None = None
    """What it does if its stream drains after it, with no `say` following: `then_listen`.
    Returns what its result adds."""

    def finish(self, reason: EndReason, played: int) -> None:
        """Set its result, given why the stream ended, or `drained` for a `say` another
        follows, and how much of it played, in bytes; and send `say.ended`. The audio it had
        played by then is what it played, and its `heard_text` is the text of its clauses
        that played through. It was `interrupted` unless every clause did."""
        if self.done.done():
            return
        drained = reason == "drained"
        if self.start is None:
            played_ms, heard = 0, 0
        else:
            if self.end is not None and self.end <= played:
                reason = "drained"
            fed = self.ends[-1] if self.ends else self.start
            played_ms = max(0, min(played, fed) - self.start) // BYTES_PER_MS
            heard = sum(end <= played for end in self.ends)
        text, clauses = self.script.text, self.script.clauses
        result: JsonObject = {
            "reason": reason,
            "played_ms": played_ms,
            "interrupted": reason != "drained" or heard < len(clauses),
            "heard_text": text[: clauses[heard - 1].end].strip() if heard else "",
        }
        if drained and self.then is not None:
            result |= self.then()
        self.done.set_result(result)
        self.session.notify("say.ended", {"address": self.address, "text": text, **result})


class Cut(Exception):
    """The run's stream never started: the run before it ended `reason`, or a newer one
    replaced it."""

    def __init__(self, reason: EndReason) -> None:
        super().__init__(reason)
        self.reason: EndReason = reason


class Run:
    """The `say`s spoken on one speaker stream, back to back, in order.

    It synthesizes each `say` on the TTS thread, `say_ahead_ms` ahead of playback at most,
    while its stream starts. A `say` followed by another ends by the pacer's schedule; the
    last once the stream drains. The stream drains only once it has sent all it was fed, so
    that a `say` queued late still follows with no gap: until then, the run is `accepting`.
    """

    def __init__(
        self,
        config: SpeechConfig,
        control: Control,
        executor: ThreadPoolExecutor,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        after: Run | None,
    ) -> None:
        """`after` is a run draining on the speaker: this one starts once it has drained,
        and is cut if it ends otherwise."""
        self.session = session
        self.says: list[Say] = []
        self.ended: asyncio.Future[EndReason] = asyncio.get_running_loop().create_future()
        """Why its stream ended; `disconnected` if it never started."""
        self._config = config
        self._control = control
        self._executor = executor
        self._device = device
        self._capability = capability
        self._after = after
        self._replaced = False
        self._stream: SpeakerStream | None = None
        """Once it has started."""
        self._closed = False
        """No `say` may join: the stream drains, or it failed to start."""
        self._wake = asyncio.Event()
        """Set when a `say` joins, or the stream starves."""
        self._fed = 0
        """Bytes fed to the stream so far."""
        self._rate = FORMAT.rate
        """The rate of the audio being fed."""
        self._resampler: Resampler | None = None
        """From `_rate` to the device's; None if they're the same."""
        self._timers: list[asyncio.TimerHandle] = []
        self._feeding: asyncio.Future[None] | None = None

    @property
    def accepting(self) -> bool:
        """Whether a `say` may still join."""
        return not self._closed and (self._stream is None or self._stream.open)

    @property
    def draining(self) -> bool:
        return self._closed and self._stream is not None and self._stream.open

    def add(self, say: Say) -> None:
        self.followed()
        self.says.append(say)
        self._wake.set()

    def followed(self) -> None:
        """A `say` follows its last one on the speaker, in it or in the run after it: the
        speaker doesn't drain after that one, which doesn't listen then."""
        if self.says:
            self.says[-1].then = None

    def replace(self) -> None:
        """A newer run cuts in: this one's stream, if it starts yet, is preempted."""
        self._replaced = True
        if self._after is not None:
            self._after.replace()

    async def speak(self) -> None:
        """Speak every `say` that joins, until the stream ends: that ends each of them."""
        starting = asyncio.ensure_future(self._start())
        feeding = self._feeding = asyncio.ensure_future(self._feed(starting))
        try:
            try:
                stream = await starting
            except (Cut, HubError) as e:
                self._fail(e)
                return
            self._stream = stream
            await asyncio.wait((feeding,))
            if not feeding.cancelled() and (e := feeding.exception()) is not None:
                _log.error("%s: say failed", stream.address, exc_info=e)
                if stream.open and not self._closed:
                    self._drain(stream)
            await self.ended
        finally:
            feeding.cancel()

    async def _start(self) -> SpeakerStream:
        if self._after is not None:
            reason = await asyncio.shield(self._after.ended)
            if reason != "drained":
                raise Cut(reason)
        if self._replaced:
            raise Cut("preempted")
        return await self._control.speaker_start(
            self.session,
            self._device,
            self._capability,
            on_starved=self._wake.set,
            on_end=self._end,
        )

    async def _feed(self, starting: asyncio.Future[SpeakerStream]) -> None:
        """Feed each `say` to the stream as it joins. Once all are fed, and the stream has
        sent them, drain it."""
        index = 0
        while True:
            if index == len(self.says):
                stream = await starting
                while index == len(self.says) and not self._control.speaker_starved(stream):
                    self._wake.clear()
                    await self._wake.wait()
                if index == len(self.says):
                    self._drain(stream)
                    return
            if index:
                self._schedule(self.says[index - 1], await starting)
            if not await self._feed_say(self.says[index], starting):
                return
            index += 1

    async def _feed_say(self, say: Say, starting: asyncio.Future[SpeakerStream]) -> bool:
        """Synthesize `say` a clause at a time, as they come in, and feed it. If the TTS
        fails, the rest of it is skipped. Returns False if the stream was cut: nothing more
        is synthesized."""
        say.start = self._fed
        while (clause := await _clause(say, len(say.ends))) is not None:
            voice = say.voice
            assert voice is not None
            synthesis = _Synthesis(self._executor, voice.engine, clause.text, voice.name)
            try:
                while not self._cut:
                    try:
                        pcm = await synthesis.next()
                    except Exception:
                        _log.exception("%s: the TTS failed on %r", say.address, clause.text)
                        say.end = self._fed
                        return True
                    if pcm is None:
                        break
                    stream = await starting
                    self._feed_pcm(stream, pcm, voice.engine.rate)
                    await self._keep_ahead(stream)
            finally:
                synthesis.close()
            if self._cut:
                return False
            say.ends.append(self._fed)
        say.end = self._fed
        return True

    @property
    def _cut(self) -> bool:
        """Whether the stream is stopping, or over: its audio goes nowhere."""
        return self._stream is not None and not self._stream.open

    def _feed_pcm(self, stream: SpeakerStream, pcm: bytes, rate: int) -> None:
        if rate != self._rate:
            self._flush(stream)
            self._rate = rate
            self._resampler = None if rate == FORMAT.rate else Resampler(rate, FORMAT.rate)
        if self._resampler is not None:
            pcm = self._resampler.process(pcm)
        self._control.speaker_feed(stream, pcm)
        self._fed += len(pcm)

    def _flush(self, stream: SpeakerStream) -> None:
        """Feed the audio the resampler still holds."""
        if self._resampler is not None:
            tail = self._resampler.flush()
            self._control.speaker_feed(stream, tail)
            self._fed += len(tail)

    async def _keep_ahead(self, stream: SpeakerStream) -> None:
        """Wait until the stream holds no more than `say_ahead_ms` of audio unsent. It sends
        in real time, so that's a known wait."""
        ahead = self._config.say_ahead_ms * BYTES_PER_MS
        if (unsent := self._control.speaker_unsent(stream)) > ahead:
            await asyncio.sleep((unsent - ahead) / BYTES_PER_MS / 1000)

    def _drain(self, stream: SpeakerStream) -> None:
        self._closed = True
        self._flush(stream)
        self._control.speaker_drain(stream)

    def _schedule(self, say: Say, stream: SpeakerStream) -> None:
        """End `say`, followed by another, once it has played by the pacer's schedule."""
        assert say.end is not None
        due = self._control.speaker_due(stream, say.end)
        delay = 0 if due is None else due + self._config.speaker_latency_ms * 1000 - now_us()
        handle = asyncio.get_running_loop().call_later(
            max(0, delay) / 1e6, say.finish, "drained", say.end
        )
        self._timers.append(handle)

    def _fail(self, e: Cut | HubError) -> None:
        """The stream never started."""
        self._closed = True
        for say in self.says:
            if isinstance(e, Cut):
                say.finish(e.reason, 0)
            elif not say.done.done():
                say.done.set_exception(e)
        self.ended.set_result(e.reason if isinstance(e, Cut) else "disconnected")

    def _end(self, end: StreamEnd) -> None:
        """The stream ended: synthesize no more, and end each `say` still playing, by how
        much of the stream played."""
        if self._feeding is not None:
            self._feeding.cancel()
        for timer in self._timers:
            timer.cancel()
        played = self._fed if end.reason == "drained" else end.stats["played_ms"] * BYTES_PER_MS
        for say in self.says:
            say.finish(end.reason, played)
        self.ended.set_result(end.reason)


async def _clause(say: Say, index: int) -> Clause | None:
    """Clause `index` of `say`'s script, once it's in; None if the script closes without
    it."""
    clauses = say.script.clauses
    while index == len(clauses) and not say.script.closed:
        say.more.clear()
        await say.more.wait()
    return clauses[index] if index < len(clauses) else None


class _Synthesis:
    """A text being synthesized on the TTS thread, a piece at a time."""

    def __init__(self, executor: ThreadPoolExecutor, engine: Tts, text: str, voice: str) -> None:
        self._executor = executor
        self._engine = engine
        self._text = text
        self._voice = voice
        self._pieces: Iterator[bytes] | None = None

    async def next(self) -> bytes | None:
        """The next piece of audio; None once there's no more."""
        loop = asyncio.get_running_loop()
        pieces = self._pieces
        if pieces is None:
            pieces = await loop.run_in_executor(
                self._executor, lambda: iter(self._engine.synthesize(self._text, self._voice))
            )
            self._pieces = pieces
        return await loop.run_in_executor(self._executor, next, pieces, None)

    def close(self) -> None:
        """Stop it, on the TTS thread once that's free: engines only ever run there."""
        close = getattr(self._pieces, "close", None)
        if close is not None:
            with contextlib.suppress(RuntimeError):  # the hub is shutting down
                self._executor.submit(close)
