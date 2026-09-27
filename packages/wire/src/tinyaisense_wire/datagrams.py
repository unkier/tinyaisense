"""UDP datagrams: a 20-byte LE header, then at most 1400 bytes of payload."""

import struct
from dataclasses import dataclass
from enum import IntEnum

from .errors import WireError
from .messages import Event, decode_event, encode_message

UDP_HEADER_SIZE = 20
MAX_UDP_PAYLOAD = 1400
FLAG_ENCRYPTED = 1 << 15
"""Reserved for per-packet encryption. MVP receivers drop such datagrams."""

_HEADER = struct.Struct("<IBBHIQ")
assert _HEADER.size == UDP_HEADER_SIZE


class DatagramKind(IntEnum):
    STREAM = 1
    EVENT_MIRROR = 2


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """One frame of a stream. `payload` is checked against the stream's format separately."""

    conn_id: int
    stream: int
    seq: int
    ts: int
    payload: bytes
    flags: int = 0


@dataclass(frozen=True, slots=True)
class EventMirror:
    """A UDP copy of an Event, sent to beat a TCP retransmit.

    The header carries the event's `seq` and `ts`, so the hub can dedupe before parsing JSON.
    """

    conn_id: int
    event: Event
    flags: int = 0


def decode_datagram(data: bytes) -> StreamFrame | EventMirror | None:
    """Decode one datagram. Returns None for an unknown kind. Raises `WireError`.

    The caller still checks the source IP and `conn_id` against the Connection.
    """
    if len(data) < UDP_HEADER_SIZE:
        raise WireError("short_datagram", f"{len(data)} bytes")
    if len(data) - UDP_HEADER_SIZE > MAX_UDP_PAYLOAD:
        raise WireError("datagram_too_large", f"{len(data) - UDP_HEADER_SIZE}-byte payload")
    conn_id, kind, stream, flags, seq, ts = _HEADER.unpack_from(data)
    if flags & FLAG_ENCRYPTED:
        raise WireError("encrypted", "no keys to decrypt with")
    payload = data[UDP_HEADER_SIZE:]
    if kind == DatagramKind.STREAM:
        if stream == 0:
            raise WireError("bad_stream", "a stream frame on stream 0")
        return StreamFrame(conn_id, stream, seq, ts, payload, flags)
    if kind == DatagramKind.EVENT_MIRROR:
        event = decode_event(payload)
        if (event.seq, event.ts) != (seq, ts):
            raise WireError("bad_message", "the mirror header doesn't match its event")
        return EventMirror(conn_id, event, flags)
    return None


def encode_datagram(datagram: StreamFrame | EventMirror) -> bytes:
    match datagram:
        case StreamFrame(conn_id, stream, seq, ts, payload, flags):
            kind = DatagramKind.STREAM
        case EventMirror(conn_id, event, flags):
            kind, stream, seq, ts = DatagramKind.EVENT_MIRROR, 0, event.seq, event.ts
            payload = encode_message(event)
    if len(payload) > MAX_UDP_PAYLOAD:
        raise ValueError(f"{len(payload)}-byte payload exceeds {MAX_UDP_PAYLOAD}")
    return _HEADER.pack(conn_id, kind, stream, flags, seq, ts) + payload
