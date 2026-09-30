"""`say`: text spoken on a speaker, on a stream the hub runs. The `say`s queued on a speaker
share one stream, back to back."""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from tinyaisense_wire import Capability, JsonObject

from ..alarm import now_us
from ..config import SpeechConfig
from ..control import Control
from ..errors import HubError
from ..resampler import Resampler, SoxrResampler, resampler_for
from ..session import Session
from ..speaker import SpeakerStream
from ..state import DeviceRecord
from ..streams import FORMAT, EndReason, StreamEnd
from .cloud import Engines, Runner
from .engines import Tts
from .text import Clause, Script

BYTES_PER_MS = FORMAT.frame_bytes // FORMAT.frame_ms
"""Of the device's audio."""

_log = logging.getLogger("tinyaisense_hub.speech")


@dataclass(frozen=True, slots=True)
class EngineVoice:
    """A Voice, as its engines know it."""

    engines: Engines[Tts]
    name: str
    fallback: str
    """The stand-in's name for it."""


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
    degraded: bool = False
    """Whether a local engine stood in for a cloud one on any of its clauses."""

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
        if self.degraded:
            result["degraded"] = True
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
        self._timers: list[asyncio.TimerHandle] = []
        self._feeding: asyncio.Future[None] | None = None
        self._stopping: asyncio.Future[StreamEnd] | None = None
        """The stop that `cut` asked for: kept from the garbage collector till it has run."""

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

    def cut(self, say: Say) -> None:
        """Stop `say`, `stopped`: skip it if none of it has gone out yet; else stop the
        stream, which cuts the `say`s after it, and those whose tail still plays."""
        if say.done.done():
            return
        if self._stream is None or say.start is None or say.start == self._fed:
            say.finish("stopped", 0)
        else:
            self._stopping = asyncio.ensure_future(self._control.stop(self._stream))

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
        fails, the rest of it is skipped, as is all of it if it's cut before any went out.
        Returns False if the stream was cut: nothing more is synthesized."""
        say.start = self._fed
        while (clause := await _clause(say, len(say.ends))) is not None:
            if say.done.done():  # cut before any of it went out
                break
            assert say.voice is not None
            synthesis = _Synthesis(say.voice, clause.text)
            try:
                while not self._cut:
                    try:
                        pcm = await synthesis.next()
                    except Exception:
                        _log.exception("%s: the TTS failed on %r", say.address, clause.text)
                        say.end = self._fed
                        return True
                    say.degraded |= synthesis.degraded
                    if pcm is None:
                        break
                    stream = await starting
                    if say.done.done():  # cut before any of it went out
                        say.end = self._fed
                        return True
                    self._control.speaker_feed(stream, pcm)
                    self._fed += len(pcm)
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

    async def _keep_ahead(self, stream: SpeakerStream) -> None:
        """Wait until the stream holds no more than `say_ahead_ms` of audio unsent. It sends
        in real time, so that's a known wait."""
        ahead = self._config.say_ahead_ms * BYTES_PER_MS
        if (unsent := self._control.speaker_unsent(stream)) > ahead:
            await asyncio.sleep((unsent - ahead) / BYTES_PER_MS / 1000)

    def _drain(self, stream: SpeakerStream) -> None:
        self._closed = True
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
    """A clause being synthesized in a Voice: by its engine, or by the stand-in if that's a
    cloud engine that fails before its first piece of audio. Then it's `degraded`."""

    def __init__(self, voice: EngineVoice, text: str) -> None:
        self._voice = voice
        self._text = text
        self._speech: _Speech | None = None
        """Once the engine that speaks it has given its first piece."""
        self.degraded = False
        """Whether the stand-in speaks it."""

    async def next(self) -> bytes | None:
        """The next piece of audio, at the device's rate; None once there's no more."""
        if self._speech is not None:
            return await self._speech.next()
        (self._speech, pcm), self.degraded = await self._voice.engines.call(self._first)
        return pcm

    async def _first(self, runner: Runner[Tts], stand_in: bool) -> tuple[_Speech, bytes | None]:
        """The clause's speech by `runner`'s engine, the `stand_in` or not, and its first
        piece."""
        voice = self._voice
        speech = _Speech(runner, self._text, voice.fallback if stand_in else voice.name)
        try:
            return speech, await speech.next()
        except BaseException:
            speech.close()
            raise

    def close(self) -> None:
        """Stop it, if it has started."""
        if self._speech is not None:
            self._speech.close()


class _Speech:
    """A text being synthesized by one engine on its thread, a piece at a time, and resampled
    there to the device's rate. The resampler lets go of the end of the text's audio once it's
    all made: then the next text starts afresh. A cloud engine has several threads: a piece it
    was given up on may still be being made on one when it's closed on another."""

    def __init__(self, runner: Runner[Tts], text: str, voice: str) -> None:
        self._runner = runner
        self._text = text
        self._voice = voice
        self._pieces: Iterator[bytes] | None = None
        self._making = threading.Lock()
        """Held on the engine's thread while it makes a piece, or stops."""
        self._resampler: Resampler | SoxrResampler | None = None
        """From the engine's rate; None if it's the device's, or once flushed."""
        if runner.engine.rate != FORMAT.rate:
            self._resampler = resampler_for(runner.engine.rate, FORMAT.rate)

    async def next(self) -> bytes | None:
        """The next piece of audio, at the device's rate; None once there's no more."""
        pieces = self._pieces
        if pieces is None:
            pieces = self._pieces = await self._runner.run(
                lambda tts: iter(tts.synthesize(self._text, self._voice))
            )
        return await self._runner.run(lambda _: self._next(pieces))

    def _next(self, pieces: Iterator[bytes]) -> bytes | None:
        """On the engine's thread."""
        with self._making:
            pcm = next(pieces, None)
        if self._resampler is None:
            return pcm
        if pcm is None:
            pcm, self._resampler = self._resampler.flush(), None
            return pcm
        return self._resampler.process(pcm)

    def close(self) -> None:
        """Stop it, on the engine's thread once that's free: engines only ever run there."""
        close = getattr(self._pieces, "close", None)
        if close is not None:
            with contextlib.suppress(RuntimeError):  # the hub is shutting down
                self._runner.thread.submit(self._stop, close)

    def _stop(self, close: Callable[[], object]) -> None:
        """On the engine's thread, once it has made the piece it's making."""
        with self._making:
            close()
