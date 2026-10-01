"""The device faults, as a hand-rolled hub sees them: clock drift and offset, WiFi drops,
reboots and garbage on the Connection."""

import asyncio
import time

import pytest
from tinyaisense_wire import Ping, Pong, StreamFrame, Welcome, WireError

from .raw_hub import CONN_ID, Online, RawHub

FORMAT = {"codec": "pcm_s16le", "rate": 16000, "channels": 1, "frame_ms": 10}


async def clock_between(online: Online) -> tuple[float, int, float]:
    """`(before, ts, after)`: the device clock `ts` from a pong, and the true time around it."""
    before = time.monotonic()
    ts = await online.clock()
    return before, ts, time.monotonic()


async def test_drift_makes_the_device_clock_run_fast_or_slow_from_when_it_is_set(
    online: Online,
) -> None:
    for ppm in (100_000, -100_000):
        online.device.faults.drift_ppm = ppm
        before0, ts0, after0 = await clock_between(online)
        await asyncio.sleep(0.2)
        before1, ts1, after1 = await clock_between(online)

        rate = 1 + ppm / 1e6
        # In µs, between the shortest and the longest the true time between the pongs can be.
        assert rate * (before1 - after0) * 1e6 <= ts1 - ts0 <= rate * (after1 - before0) * 1e6


async def test_a_drift_change_never_steps_the_clock(online: Online) -> None:
    await asyncio.sleep(0.1)  # well past 0, so that halving its rate since boot would show
    before = await online.clock()

    online.device.faults.drift_ppm = -500_000

    online.connection.send(Ping(1))
    pong = await online.connection.receive()
    assert isinstance(pong, Pong)
    assert pong.ts >= before


@pytest.mark.parametrize("ppm", [100_000, -100_000])
async def test_drift_runs_the_mic_fast_or_slow_too(online: Online, ppm: int) -> None:
    online.device.faults.drift_ppm = ppm
    before = time.monotonic()
    await online.ok("mic", "start", {"stream": 5, "format": FORMAT})
    await asyncio.sleep(0.3)

    result = await online.ok("mic", "stop")

    # 10 ms frames, on a clock `rate` times as fast as true time.
    rate = 1 + ppm / 1e6
    frames = result["last_seq"] + 1
    assert 0.3 * rate * 100 - 1 <= frames <= (time.monotonic() - before) * rate * 100 + 1


async def test_drift_runs_the_speaker_fast_or_slow_too(online: Online) -> None:
    online.device.faults.drift_ppm = -500_000  # half speed: a 10 ms frame lasts 20 ms
    speaker = online.device.speaker
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})
    for seq in range(10):
        online.udp.send(StreamFrame(CONN_ID, 1, seq, 0, bytes(320)), online.device.udp_port)
    await online.ok("speaker", "drain", {"last_seq": 9})

    await online.device.wait_for(lambda: len(speaker.captured) == 10)

    gaps = [b.at - a.at for a, b in zip(speaker.captured, speaker.captured[1:], strict=False)]
    assert gaps == pytest.approx([0.02] * 9, abs=0.003)


async def test_a_reboot_drops_the_connection_unannounced_and_boots_afresh(
    online: Online, raw_hub: RawHub
) -> None:
    device = online.device
    old = online.connection
    assert old.hello is not None
    await online.ok("speaker", "set_volume", {"level": 30})
    device.faults.clock_offset_us = 3_600_000_000  # an hour

    device.faults.reboot(down_ms=200, reason="panic")

    assert device.state == "rebooting"
    connection = await raw_hub.accept()
    # Its network stack was gone: the old Connection gets an RST, not a FIN.
    with pytest.raises(ConnectionResetError):
        await old.until_closed()
    assert connection.hello is not None
    assert connection.hello.udp_port != old.hello.udp_port  # a fresh socket
    online.connection = connection
    online.next_id = 1
    connection.send(Welcome("kitchen", CONN_ID + 1, online.udp.port))
    await device.wait_for(lambda: device.state == "online")
    diag = await online.ok(None, "diag")
    assert (diag["reset_reason"], diag["uptime_s"], diag["reconnects"]) == ("panic", 0, 0)
    assert 3_600_000_000 <= await online.clock() < 3_601_000_000
    assert device.speaker.level == 80  # the volume is lost: the hub sets it again


async def test_a_wifi_drop_shows_offline_and_gives_up_the_connection_with_no_word_to_the_hub(
    online: Online, raw_hub: RawHub
) -> None:
    device = online.device
    old = online.connection
    loop = asyncio.get_running_loop()

    device.faults.wifi_drop(500)

    dropped = loop.time()
    assert (device.state, device.status) == ("connecting", "offline")
    old.send(Ping(1))  # lost on the way
    connection = await raw_hub.accept()
    assert loop.time() - dropped >= 0.5  # it waits for WiFi
    assert device.status == "connecting"
    connection.send(Welcome("kitchen", CONN_ID + 1, online.udp.port))
    # Once the hub has answered, the device closes the old Connection: the hub has by now.
    assert await old.until_closed() == []


async def test_datagrams_are_lost_while_wifi_is_down_until_it_is_back(online: Online) -> None:
    device = online.device
    await online.ok("speaker", "start", {"stream": 1, "format": FORMAT})

    device.faults.wifi_down = True
    online.udp.send(StreamFrame(CONN_ID, 1, 0, 0, bytes(320)), device.udp_port)
    await asyncio.sleep(0.05)

    assert device.faults.udp_down.dropped == 1
    assert device.stats == {}  # not even a frame of a stream it no longer has
    device.faults.wifi_down = False
    online.udp.send(StreamFrame(CONN_ID, 1, 1, 0, bytes(320)), device.udp_port)
    await asyncio.sleep(0.05)
    assert device.stats == {"bad_source": 1}  # in, though it has no Connection yet


async def test_send_garbage_sends_a_json_frame_that_holds_no_json(online: Online) -> None:
    online.device.faults.send_garbage()

    with pytest.raises(WireError) as garbage:
        await online.connection.receive()

    assert garbage.value.code == "bad_json"


async def test_send_garbage_sends_any_bytes_it_is_given(online: Online) -> None:
    online.device.faults.send_garbage(b"\x01\x00\xff\xff")  # a frame header of 65535 bytes

    with pytest.raises(WireError) as garbage:
        await online.connection.receive()

    assert garbage.value.code == "frame_too_large"
