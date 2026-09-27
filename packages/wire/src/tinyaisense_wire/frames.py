"""TCP framing: a 4-byte header `type u8, flags u8, len u16 LE`, then the body."""

import struct
from dataclasses import dataclass
from enum import IntEnum

from .errors import WireError

MAX_FRAME_BODY = 4096
_HEADER = struct.Struct("<BBH")


class FrameType(IntEnum):
    JSON = 1
    PAYLOAD = 2
    """A bulk payload chunk: specced, not built in the MVP."""
    STREAM = 3
    """Reserved for in-band stream frames."""


@dataclass(frozen=True, slots=True)
class Frame:
    type: int
    body: bytes
    flags: int = 0


def encode_frame(frame: Frame) -> bytes:
    if len(frame.body) > MAX_FRAME_BODY:
        raise ValueError(f"frame body of {len(frame.body)} bytes exceeds {MAX_FRAME_BODY}")
    return _HEADER.pack(frame.type, frame.flags, len(frame.body)) + frame.body


class FrameDecoder:
    """Splits a TCP byte stream into frames, whatever the chunking. Sans-IO.

    Frames come out one at a time, so the caller handles each before the next header is
    even read: on a broken stream, the first error wins. After a `WireError`, the stream is
    broken and the decoder must not be used again.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer += data

    def next_frame(self) -> Frame | None:
        """Pop the next complete frame, or None until more bytes arrive. Raises `WireError`."""
        buffer = self._buffer
        if len(buffer) < _HEADER.size:
            return None
        ftype, flags, length = _HEADER.unpack_from(buffer)
        if length > MAX_FRAME_BODY:
            raise WireError("frame_too_large", f"len {length} exceeds {MAX_FRAME_BODY}")
        end = _HEADER.size + length
        if len(buffer) < end:
            return None
        body = bytes(buffer[_HEADER.size : end])
        del buffer[:end]
        return Frame(ftype, body, flags)

    def close(self) -> None:
        """Signal the end of the stream. Raises `WireError` if it ended inside a frame."""
        if self._buffer:
            raise WireError("truncated", f"the stream ended {len(self._buffer)} bytes into a frame")
