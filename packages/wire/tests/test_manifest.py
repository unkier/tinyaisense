import logging

import pytest
from tinyaisense_wire import ControlDecoder, Hello

HELLO_WITH_A_THERMOMETER = (
    b'{"type":"hello","token":null,"udp_port":50123,"manifest":{"protocol":1,'
    b'"device_id":"a1b2c3d4e5f6","board":"m5stack-atom-echo","firmware":"0.1.0",'
    b'"capabilities":[{"id":"button","kind":"button"},{"id":"thermo","kind":"thermometer"}],'
    b'"exclusive":[]}}'
)


def test_a_capability_of_an_unknown_kind_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = bytes([1, 0]) + len(HELLO_WITH_A_THERMOMETER).to_bytes(2, "little")
    [hello] = ControlDecoder().feed(frame + HELLO_WITH_A_THERMOMETER)

    assert isinstance(hello, Hello)
    assert [cap.id for cap in hello.manifest.supported] == ["button"]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "thermometer" in warning.getMessage()
    assert "a1b2c3d4e5f6" in warning.getMessage()
