from typing import Literal, TypeAlias

WireErrorCode: TypeAlias = Literal[
    "frame_too_large",
    "truncated",
    "bad_json",
    "bad_message",
    "protocol_mismatch",
    "short_datagram",
    "datagram_too_large",
    "encrypted",
    "bad_stream",
    "partial_sample",
    "short_frame",
    "oversized_frame",
]
"""The decode errors of docs/protocol/device.md#decode-errors."""


class WireError(Exception):
    """Bytes from the wire broke the protocol.

    On TCP the receiver closes the Connection; on UDP it drops and counts the datagram.
    """

    def __init__(self, code: WireErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code: WireErrorCode = code
        self.detail = detail
