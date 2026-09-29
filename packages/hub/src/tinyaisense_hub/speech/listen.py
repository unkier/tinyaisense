"""`listen`: an Utterance heard on a mic, on a stream the hub runs.

The VAD rates the mic's audio as it comes in, on its own thread, and the listen core
(`listening`) cuts it into segments and says when the listen ends. Each segment is
transcribed, and the end-of-turn model judges it, at once and in parallel.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from tinyaisense_wire import Capability, JsonObject

from ..alarm import Alarm, now_us
from ..config import Timing
from ..control import Control
from ..errors import HubError
from ..keepalive import Tick
from ..mic import MicStream
from ..session import Session, UtteranceOptIn
from ..state import DeviceRecord
from ..streams import FORMAT, StreamEnd
from .engines import Engine, Stt, Transcript, Turn, Vad
from .listening import End, EndReason, Listening, Segment, Stop, TurnVerdict, Window

type Trigger = Literal["listen", "then_listen", "button"]
"""What started an Utterance."""

TURN_MS = 8000
"""The end-of-turn model hears the last this much of the audio."""
TURN_COMPLETE = 0.5
"""The end-of-turn model's rating from which a turn is complete."""
ANSWERABLE: frozenset[EndReason] = frozenset({"end_of_turn", "max_duration", "button"})
"""An Utterance that ends so leaves the agent something to answer: the device shows
`thinking`."""

_SAMPLE = 2
"""Bytes per sample."""

_log = logging.getLogger("tinyaisense_hub.speech")


@dataclass(frozen=True, slots=True)
class Ears:
    """The engines a listen hears with, and the threads they run on."""

    vad: Vad
    turn: Turn | None
    stt: Stt
    executors: Mapping[type[Engine], ThreadPoolExecutor]


class Listen:
    """One Utterance heard on a mic, from `start` until `done`.

    It's among its Session's `listens` until then, and holds its `handle`, if it delivers
    audio."""

    def __init__(
        self,
        ears: Ears,
        control: Control,
        timing: Timing,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        *,
        trigger: Trigger,
        lang: str | None,
        utterances: UtteranceOptIn,
    ) -> None:
        self.session = session
        self.id = session.new_utterance_id()
        self.handle = session.new_handle() if utterances.audio else None
        """The handle its audio goes to the agent on, if it does."""
        session.listens.add(self)
        self.done: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        """The `utterance.end` params, once it's delivered."""
        self._ears = ears
        self._control = control
        self._timing = timing
        self._device = device
        self._capability = capability
        self._address = f"{device.name}/{capability.id}"
        self._trigger: Trigger = trigger
        self._lang = lang
        self._utterances = utterances
        self._stream: MicStream | None = None
        self._core: Listening | None = None
        """From the mic's start."""
        self._stop: EndReason | None = None
        """Why it ends, if it's stopped before its mic starts."""
        self._alarm = Alarm(lambda now: self._run(Tick(), now))
        self._audio = bytearray()
        """What the mic heard, at the device's rate."""
        self._unrated = 0
        """Where in `_audio` the VAD has yet to rate from, in bytes."""
        self._vad_state: object = None
        self._rated: deque[asyncio.Future[float]] = deque()
        """The VAD's ratings, in the order of the audio, as they come."""
        self._segments: list[tuple[int, asyncio.Future[Transcript | None]]] = []
        """Each segment's length in samples, and its transcript to come."""
        self._tasks: set[asyncio.Future[None]] = set()

    async def start(self) -> None:
        """Open the mic. Raises `HubError`, as `done` does, the listen being over."""
        try:
            await self._control.mic_start(
                self.session,
                self._device,
                self._capability,
                audio=self.handle,
                rate=self._utterances.rate,
                tap=self._hear,
                on_started=self._started,
                on_end=self._mic_ended,
            )
        except BaseException as e:
            self.session.listens.discard(self)
            self.done.set_exception(e)
            self.done.exception()  # retrieved: it's raised here
            raise

    async def start_or_end(self) -> None:
        """Open the mic for an Utterance the agent knows of already, from `then_listen`: if
        it fails to open, deliver it `disconnected`, with nothing heard."""
        try:
            await self.start()
        except HubError as e:
            _log.info("%s: the mic didn't start: %s", self._address, e)
            self.session.notify("utterance.end", self._result("disconnected", [], 0, 0))

    def stop(self, reason: EndReason) -> None:
        """End the Utterance with `reason`, from outside."""
        if self._core is None:
            self._stop = self._stop or reason
        else:
            self._run(Stop(reason))

    def _started(self, stream: MicStream) -> None:
        self._stream = stream
        params: JsonObject = {
            "utterance_id": self.id,
            "address": self._address,
            "trigger": self._trigger,
        }
        if self.handle is not None:
            params["handle"] = self.handle
        self.session.notify("utterance.start", params)
        self._core = Listening(now_us(), self._timing, window=self._ears.vad.window)
        self._run(Stop(self._stop) if self._stop is not None else Tick())

    def _hear(self, pcm: bytes) -> None:
        """A frame from the mic: have the VAD rate each whole window it completes."""
        self._audio += pcm
        window = self._ears.vad.window * _SAMPLE
        loop = asyncio.get_running_loop()
        while len(self._audio) - self._unrated >= window:
            chunk = bytes(self._audio[self._unrated : self._unrated + window])
            self._unrated += window
            rating = loop.run_in_executor(self._ears.executors[Vad], self._rate, chunk)
            rating.add_done_callback(self._rating)
            self._rated.append(rating)

    def _rate(self, pcm: bytes) -> float:
        """On the VAD's thread."""
        vad = self._ears.vad
        if self._vad_state is None:
            self._vad_state = vad.new_state()
        return vad.probability(self._vad_state, pcm)

    def _rating(self, _: asyncio.Future[float]) -> None:
        while self._rated and self._rated[0].done():
            rating = self._rated.popleft()
            if rating.cancelled():
                continue
            if (e := rating.exception()) is not None:
                _log.error("%s: the VAD failed", self._address, exc_info=e)
                self._run(Window(0.0))
            else:
                self._run(Window(rating.result()))

    def _mic_ended(self, end: StreamEnd) -> None:
        self.stop("disconnected" if end.reason == "disconnected" else "stopped")

    def _run(self, input: Window | TurnVerdict | Stop | Tick, now: int | None = None) -> None:
        assert self._core is not None
        now = now_us() if now is None else now
        for action in self._core.handle(input, now):
            match action:
                case Segment():
                    self._segment(action)
                case End(reason):
                    self._spawn(self._end(reason))
        self._alarm.set(self._core.next_deadline(), now)

    def _segment(self, segment: Segment) -> None:
        """Transcribe it, and ask the end-of-turn model, at once."""
        pcm = bytes(self._audio[segment.start * _SAMPLE : segment.end * _SAMPLE])
        before = self._segments[-1][1] if self._segments else None
        transcript = asyncio.ensure_future(self._transcribe(pcm, before))
        self._segments.append((segment.end - segment.start, transcript))
        if segment.turn and self._ears.turn is not None:
            start = max(0, segment.end - TURN_MS * FORMAT.rate // 1000)
            turn = bytes(self._audio[start * _SAMPLE : segment.end * _SAMPLE])
            self._spawn(self._judge(segment.index, turn))

    async def _transcribe(
        self, pcm: bytes, before: asyncio.Future[Transcript | None] | None
    ) -> Transcript | None:
        """The segment's transcript, if the agent wants it, sent as a partial after the
        segments before it; None if the STT failed."""
        if not self._utterances.transcript:
            return None
        loop = asyncio.get_running_loop()
        try:
            transcript = await loop.run_in_executor(
                self._ears.executors[Stt], self._ears.stt.transcribe, pcm, self._lang
            )
        except Exception:
            _log.exception("%s: the STT failed", self._address)
            transcript = None
        if before is not None:
            await asyncio.wait((before,))
        if transcript is not None and transcript.text:
            self.session.notify(
                "transcript.partial",
                {"utterance_id": self.id, "text": transcript.text, "lang": transcript.lang},
            )
        return transcript

    async def _judge(self, index: int, pcm: bytes) -> None:
        turn = self._ears.turn
        assert turn is not None
        loop = asyncio.get_running_loop()
        try:
            complete = await loop.run_in_executor(self._ears.executors[Turn], turn.complete, pcm)
        except Exception:
            _log.exception("%s: the end-of-turn model failed", self._address)
            return
        self._run(TurnVerdict(index, complete >= TURN_COMPLETE))

    async def _end(self, reason: EndReason) -> None:
        """Close the mic, and deliver the Utterance once its segments are transcribed."""
        assert self._stream is not None
        end = await self._control.stop(self._stream)
        heard = [(length, await transcript) for length, transcript in self._segments]
        frames = end.stats["frames"] + end.stats["skipped"]
        result = self._result(reason, heard, frames * FORMAT.frame_ms, end.stats["lost"])
        self.session.notify("utterance.end", result, after=self.handle)
        if reason in ANSWERABLE:
            self._control.utterance_delivered(self._device)
        self.session.listens.discard(self)
        if not self.done.done():  # its start failed, though its mic opened
            self.done.set_result(result)

    def _result(
        self,
        reason: EndReason,
        heard: list[tuple[int, Transcript | None]],
        duration_ms: int,
        lost: int,
    ) -> JsonObject:
        """The `utterance.end` params, given each segment's length and transcript. Its
        transcript joins theirs, and its lang is that of the longest one with words."""
        result: JsonObject = {"utterance_id": self.id, "reason": reason}
        if self._utterances.transcript:
            transcripts = [(length, t) for length, t in heard if t is not None and t.text]
            result["transcript"] = " ".join(t.text for _, t in transcripts)
            if transcripts:
                result["lang"] = max(transcripts, key=lambda length_t: length_t[0])[1].lang
        result["stats"] = {"duration_ms": duration_ms, "lost": lost}
        return result

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
