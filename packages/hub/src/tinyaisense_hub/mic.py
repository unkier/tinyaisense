"""Agent mic streams, assembled from the device's frames: docs/protocol/audio.md#mic-stream."""

import asyncio
import logging
from typing import TYPE_CHECKING

from tinyaisense_wire import KINDS, JsonObject, SchemaError, StreamFrame, validate

from .alarm import Alarm, now_us
from .assembler import Deliver, Finished, Frame, Last, MicAssembler
from .devices import DeviceConnection
from .keepalive import Tick
from .resampler import Resampler
from .streams import FORMAT, Stream

if TYPE_CHECKING:
    from .session import Session  # it holds its streams

STOP_RESULT = KINDS["mic"].commands["stop"].result

_log = logging.getLogger("tinyaisense_hub.mic")


class MicStream(Stream):
    """One raw agent mic stream. Its frames reach the agent as a timeline at its rate, from
    `started` on: frames that beat the `start` reply wait for it. After the `stop` reply's
    `last_seq`, the stream is `finished` once its tail is in, or once it has waited `tail` µs
    for it."""

    def __init__(
        self,
        handle: int,
        session: Session,
        conn: DeviceConnection,
        cap: str,
        stream_id: int,
        address: str,
        rate: int,
        *,
        tail: int,
    ) -> None:
        super().__init__(handle, session, conn, cap, stream_id, address)
        self.dropped = 0
        """Frames the device dropped from its queue, from its `stop` reply."""
        self.finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._assembler = MicAssembler(FORMAT, tail=tail)
        self._resampler = None if rate == FORMAT.rate else Resampler(FORMAT.rate, rate)
        """From the device's rate to the agent's; None if they're the same. Each frame comes
        out as one frame, and the last 1 ms of the stream stays in it."""
        self._alarm = Alarm(self._tick)
        self._waiting: list[Deliver] | None = []
        """What the timeline holds before the `start` reply; None after it."""

    def started(self) -> None:
        """The device replied to `start`: the agent gets the timeline from now on."""
        waiting, self._waiting = self._waiting, None
        for frame in waiting or []:
            self.session.audio(self.handle, frame.index, frame.pcm)

    def frame(self, frame: StreamFrame, now: int) -> None:
        self._run(Frame(frame.seq, frame.payload), now)

    def stopped(self, reply: JsonObject | BaseException) -> None:
        """The device replied to `stop`: wait for the tail up to its `last_seq`. If the `stop`
        failed, the stream is finished as it stands."""
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
                case Deliver(index, pcm):
                    if self._resampler is not None:
                        action = Deliver(index, self._resampler.process(pcm))
                    if self._waiting is not None:
                        self._waiting.append(action)
                    else:
                        self.session.audio(self.handle, action.index, action.pcm)
                case Finished():
                    self.close()
                    return
        self._alarm.set(self._assembler.next_deadline(), now)
