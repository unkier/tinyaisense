"""What every agent stream shares, speaker or mic: docs/protocol/agent.md#streams."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tinyaisense_wire import AudioFormat, JsonObject

from .devices import DeviceConnection

if TYPE_CHECKING:
    from .session import Session  # it holds its streams

type EndReason = Literal["drained", "stopped", "preempted", "reflex", "disconnected", "overflow"]

FORMAT = AudioFormat("pcm_s16le", 16000, 1, 10)
"""What the hub streams with every device: 10 ms frames of 16 kHz mono PCM."""


@dataclass(frozen=True, slots=True)
class StreamEnd:
    reason: EndReason
    stats: JsonObject

    def to_json(self) -> JsonObject:
        return {"reason": self.reason, "stats": self.stats}


class Stream:
    """One stream of a Session, from the device's `start` reply to its end."""

    def __init__(
        self,
        handle: int | None,
        session: Session,
        conn: DeviceConnection,
        cap: str,
        stream_id: int,
        address: str,
        rate: int,
    ) -> None:
        self.handle = handle
        """What the agent calls it; None if the hub runs it for the agent, as for a `say`,
        and the agent doesn't see it."""
        self.session = session
        self.conn = conn
        self.cap = cap
        self.stream_id = stream_id
        """The device stream id, 1-127."""
        self.address = address
        self.rate = rate
        """The rate on the agent's side."""
        self.ended: asyncio.Future[StreamEnd] = asyncio.get_running_loop().create_future()
        self.stopping = False
        """A stop is on its way."""

    @property
    def open(self) -> bool:
        """Whether the stream is neither stopping nor over."""
        return not self.stopping and not self.ended.done()
