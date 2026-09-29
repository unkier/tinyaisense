"""Agent mic streams, assembled from the device's frames: docs/protocol/audio.md#mic-stream."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tinyaisense_wire import KINDS, JsonObject, SchemaError, StreamFrame, validate

from .alarm import Alarm, now_us
from .assembler import Deliver, Finished, Frame, Last, MicAssembler
from .devices import DeviceConnection
from .keepalive import Tick
from .resampler import Resampler
from .streams import FORMAT, Stream, StreamEnd

if TYPE_CHECKING:
    from .session import Session  # it holds its streams

STOP_RESULT = KINDS["mic"].commands["stop"].result
STARTED = KINDS["mic"].events["started"].data

_log = logging.getLogger("tinyaisense_hub.mic")


@dataclass(frozen=True, slots=True)
class Listener:
    """What hears a mic stream the hub runs for a listen."""

    audio: int | None
    """The handle the frames reach the agent on, if they do."""
    rate: int
    """Their rate, on the agent's side."""
    hear: Callable[[bytes], None]
    """Takes each frame, at the device's rate, from just after `started`."""
    started: Callable[[MicStream], None]
    ended: Callable[[StreamEnd], None]
    held: Callable[[], None]
    """Called as the human presses the button for it: push-to-talk, from then on."""
    released: Callable[[bool], None]
    """Called as the human lets go, with whether the press was a click."""


class MicStream(Stream):
    """One mic stream. Its frames reach the agent on its `audio` handle, if it has one, as a
    timeline at its rate, and its `listener`, if it has one, at the device's rate: both from
    `started` on, as frames that beat the `start` reply wait for it. After the `stop` reply's
    `last_seq`, the stream is `finished` once its tail is in, or once it has waited `tail` µs
    for it."""

    listener: Listener | None = None
    """What hears it, for a listen."""
    held = False
    """Whether the human holds the button for it."""
    reflex = False
    """Whether the Reflex opened it, on a device-range id: it closes when the button lets go."""

    def __init__(
        self,
        handle: int | None,
        session: Session,
        conn: DeviceConnection,
        cap: str,
        stream_id: int,
        address: str,
        rate: int,
        *,
        audio: int | None,
        tail: int,
    ) -> None:
        super().__init__(handle, session, conn, cap, stream_id, address, rate)
        self.audio = audio
        """The handle its frames reach the agent on: its own, for a raw stream; the
        Utterance's, for a listen's; None if they don't."""
        self.dropped = 0
        """Frames the device dropped from its queue, from its `stop` reply."""
        self.finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._assembler = MicAssembler(FORMAT, tail=tail)
        self._resampler = (
            None if audio is None or rate == FORMAT.rate else Resampler(FORMAT.rate, rate)
        )
        """From the device's rate to the agent's; None if they're the same, or the agent
        gets no frames. Each frame comes out as one frame, and the last 1 ms of the stream
        stays in it."""
        self._alarm = Alarm(self._tick)
        self._waiting: list[Deliver] | None = []
        """What the timeline holds before the `start` reply, or the Reflex's `started`; None
        after it."""
        self._last = False
        """Whether the device has said where the stream ends."""

    @classmethod
    def listened(
        cls,
        session: Session,
        conn: DeviceConnection,
        cap: str,
        stream_id: int,
        address: str,
        listener: Listener,
        *,
        tail: int,
    ) -> MicStream:
        """A stream the hub runs for a listen, which `listener` hears."""
        stream = cls(
            None,
            session,
            conn,
            cap,
            stream_id,
            address,
            listener.rate,
            audio=listener.audio,
            tail=tail,
        )
        stream.listener = listener
        stream.on_end = listener.ended
        return stream

    def started(self) -> None:
        """The device replied to `start`, or said the Reflex started it: the frames go out from
        now on."""
        if self._waiting is None:
            return
        if self.listener is not None:
            self.listener.started(self)
        waiting, self._waiting = self._waiting, None
        for frame in waiting:
            self._deliver(frame)

    def frame(self, frame: StreamFrame, now: int) -> None:
        self._run(Frame(frame.seq, frame.payload), now)

    def stopped(self, reply: JsonObject | BaseException) -> None:
        """The device replied to `stop`, or said the Reflex stopped the stream: wait for the
        tail up to its `last_seq`. If the `stop` failed, the stream is finished as it stands.
        Once the device has said where the stream ends, it has: what it says after is
        ignored."""
        if self._last:
            return
        self._last = True
        if isinstance(reply, BaseException):
            self.close()
            return
        try:
            validate(STOP_RESULT, reply, "result")
        except SchemaError as e:
            _log.warning("%s: bad mic stop result: %s", self.address, e)
            self.close()
            return
        self.dropped = reply["dropped"]
        self._run(Last(reply["last_seq"]), now_us())

    def close(self) -> None:
        """Take no more frames: the stream is finished as it stands."""
        self._alarm.cancel()
        if not self.finished.done():
            self.finished.set_result(None)
            a = self._assembler
            _log.info(
                "%s: mic stream %d: %d frames, %d zero-filled, %d late, dropped %s",
                self.address,
                self.stream_id,
                a.frames,
                a.filled,
                a.late,
                dict(a.rejected),
            )

    def stats(self, skipped: int) -> JsonObject:
        """`{frames, lost, dropped, skipped}`, given the frames the agent's link skipped. The
        frames the device dropped leave gaps too, so they're zero-filled but not `lost`."""
        a = self._assembler
        return {
            "frames": a.frames - skipped,
            "lost": max(0, a.filled - self.dropped),
            "dropped": self.dropped,
            "skipped": skipped,
        }

    def _tick(self, now: int) -> None:
        self._run(Tick(), now)

    def _run(self, input: Frame | Last | Tick, now: int) -> None:
        if self.finished.done():
            return
        for action in self._assembler.handle(input, now):
            match action:
                case Deliver():
                    if self._waiting is not None:
                        self._waiting.append(action)
                    else:
                        self._deliver(action)
                case Finished():
                    self.close()
                    return
        self._alarm.set(self._assembler.next_deadline(), now)

    def _deliver(self, frame: Deliver) -> None:
        if self.listener is not None:
            self.listener.hear(frame.pcm)
        if self.audio is not None:
            pcm = frame.pcm if self._resampler is None else self._resampler.process(frame.pcm)
            self.session.audio(self.audio, frame.index, pcm)
