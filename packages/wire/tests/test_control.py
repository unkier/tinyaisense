"""The control stream decoder, beyond what one golden vector at a time can show."""

import pytest
from tinyaisense_wire import ControlDecoder, Ping, WireError, encode_control

OVERSIZED_HEADER = bytes([1, 0, 0xFF, 0xFF])


def test_messages_before_a_bad_frame_reach_the_caller() -> None:
    messages = ControlDecoder().feed(encode_control(Ping(1)) + OVERSIZED_HEADER)
    assert next(messages) == Ping(1)
    with pytest.raises(WireError) as failure:
        next(messages)
    assert failure.value.code == "frame_too_large"
