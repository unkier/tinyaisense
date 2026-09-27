"""The control stream: JSON messages in TCP frames."""

from collections.abc import Iterator

from .frames import Frame, FrameDecoder, FrameType, encode_frame
from .messages import Message, decode_message, encode_message


def encode_control(message: Message) -> bytes:
    """Encode a message as one canonical type-1 frame."""
    return encode_frame(Frame(FrameType.JSON, encode_message(message)))


class ControlDecoder:
    """Turns a device TCP byte stream into messages, whatever the chunking. Sans-IO.

    Frames of other types and messages of unknown `type` are skipped. Any `WireError` it
    raises is a protocol error: close the Connection and don't feed it again. The messages
    before the bad frame have been yielded by then, so none is lost.
    """

    def __init__(self) -> None:
        self._frames = FrameDecoder()

    def feed(self, data: bytes) -> Iterator[Message]:
        """Buffer `data` and yield the messages it completes, in order.

        Decoding is lazy: each message is yielded before the next frame is looked at.
        Messages left unconsumed come out of the next `feed`.
        """
        self._frames.feed(data)
        return self._messages()

    def _messages(self) -> Iterator[Message]:
        while (frame := self._frames.next_frame()) is not None:
            if frame.type == FrameType.JSON:
                message = decode_message(frame.body)
                if message is not None:
                    yield message

    def close(self) -> None:
        """Signal the end of the stream. Raises `WireError` if it ended inside a frame."""
        self._frames.close()
