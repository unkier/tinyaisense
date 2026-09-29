"""Simulated devices, each speaking the device protocol to a real hub as its firmware would."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import random
import socket
import time
from collections import Counter, deque
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast

from tinyaisense_wire import (
    DEVICE_COMMANDS,
    KINDS,
    MDNS_SERVICE,
    AudioFormat,
    Capability,
    Command,
    ControlDecoder,
    Error,
    Event,
    EventMirror,
    FailedReply,
    Hello,
    JsonObject,
    Manifest,
    Message,
    Pending,
    Ping,
    Pong,
    Reply,
    SchemaError,
    StreamFrame,
    Welcome,
    WireError,
    decode_datagram,
    encode_datagram,
    validate,
)

from .backoff import Backoff
from .button import Edge, SimButton
from .discovery import Mdns, Resolve
from .faults import ConnectionSocket, Faults
from .led import Light, SimLed
from .mic import SimMic
from .profiles import PROFILES, Board
from .speaker import SimSpeaker
from .status import Status, StatusLayers

FIRMWARE = f"sim-{version('tinyaisense-sim')}"
DEAD_AFTER_MS = 6000
"""A Connection with no inbound frame for this long is dead."""
OUTPUT_LATENCY_MS = 30
"""How long audio takes from the jitter buffer to the ear: the DMA."""
_CLOSED_STREAMS = 16
"""The closed stream ids a Connection remembers, to drop their late frames silently."""
MIRROR_GAP_MS = 5
"""An event's second UDP mirror goes out this long after its first."""

DeviceState = Literal["connecting", "pending", "online", "closed"]

_log = logging.getLogger("tinyaisense_sim")


def device_id_for(name: str) -> str:
    """`02` plus 5 bytes of a hash of the instance name, so Pairing survives across runs."""
    return "02" + hashlib.sha256(name.encode()).hexdigest()[:10]


class Simulator:
    """Runs simulated devices in this process against one hub."""

    def __init__(
        self,
        hub: str | tuple[str, int] | None = None,
        state_dir: Path | None = None,
        *,
        seed: int = 0,
        dead_after_ms: int = DEAD_AFTER_MS,
        mdns_service: str = MDNS_SERVICE,
        output_latency_ms: int = OUTPUT_LATENCY_MS,
    ) -> None:
        """`hub` is the hub's device address, `host:port`.

        Without one, devices find the hub over mDNS as `mdns_service`, as firmware does.
        Tokens live in `state_dir` if given, and only in memory otherwise. A device drops a
        Connection that brings no frame for `dead_after_ms`. A speaker's audio is heard
        `output_latency_ms` after it leaves the jitter buffer. `seed` drives every random
        choice, the UDP faults included.
        """
        self._mdns: Mdns | None = None
        if hub is None:
            self._mdns = Mdns(mdns_service)
            self._resolve: Resolve = self._mdns.resolve
            self._udp_host = "0.0.0.0"
        else:
            address = _address(hub)
            self._resolve = lambda: _static(address)
            self._udp_host = "::" if ":" in address[0] else "0.0.0.0"
        self._tokens = _Tokens(state_dir)
        self._seed = seed
        self._dead_after = dead_after_ms / 1000
        self._output_latency_ms = output_latency_ms
        self._devices: list[SimDevice] = []

    @property
    def devices(self) -> tuple[SimDevice, ...]:
        return tuple(self._devices)

    async def add(self, board: str, name: str) -> SimDevice:
        """Start a simulated `board` device, identified by its instance `name`.

        Returns once the hub has answered it with `welcome` or `pending`. Until then the
        device retries as firmware would, forever: bound the wait with `asyncio.timeout`.
        """
        if board not in PROFILES:
            raise ValueError(f"unknown board {board!r}; known: {', '.join(PROFILES)}")
        profile = PROFILES[board]
        device = SimDevice(
            name,
            profile,
            profile.manifest(device_id_for(name), FIRMWARE),
            self._resolve,
            self._udp_host,
            self._tokens,
            # Its own streams, so that one device's randomness doesn't shift another's.
            random.Random(f"{self._seed}:{name}"),
            random.Random(f"{self._seed}:{name}:udp"),
            random.Random(f"{self._seed}:{name}:udp-up"),
            self._dead_after,
            self._output_latency_ms,
            self._devices.remove,
        )
        self._devices.append(device)
        await device.start()
        await device.wait_for(lambda: device.state in ("pending", "online"))
        return device

    async def close(self) -> None:
        for device in list(self._devices):
            await device.close()
        if self._mdns is not None:
            await self._mdns.close()


class SimDevice:
    """One simulated device. Get one from `Simulator.add`."""

    def __init__(
        self,
        instance: str,
        board: Board,
        manifest: Manifest,
        resolve: Resolve,
        udp_host: str,
        tokens: _Tokens,
        rng: random.Random,
        udp_rng: random.Random,
        udp_up_rng: random.Random,
        dead_after: float,
        output_latency_ms: int,
        on_close: Callable[[SimDevice], None],
    ) -> None:
        self.instance = instance
        self.manifest = manifest
        self.state: DeviceState = "connecting"
        self.name: str | None = None
        """The hub-owned name from the last `welcome`."""
        self.last_error: str | None = None
        """The `code` of the last `error` from the hub."""
        self.conn_id: int | None = None
        """From `welcome`, while the Connection lasts."""
        self.reconnects = 0
        """Connections the hub answered, after the first."""
        self.faults = Faults(udp_rng, udp_up_rng, self._mic_tx_resumed)
        self.stats: Counter[str] = Counter()
        """Datagrams dropped, by why: `bad_source`, `bad_conn_id`, `unknown_stream`, or a
        datagram or frame check's error code. Closed streams' late frames aren't counted."""
        self._board = board
        self._resolve = resolve
        self._udp_host = udp_host
        self._tokens = tokens
        self._backoff = Backoff(rng)
        self._dead_after = dead_after
        self._on_close = on_close
        self._boot_ns = time.monotonic_ns()
        self._changed = asyncio.Event()
        self._udp: asyncio.DatagramTransport | None = None
        self._conn: ConnectionSocket | None = None
        self._hub_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
        """The TCP peer IP of the Connection: datagrams come from there."""
        self._hub_udp: tuple[str, int] | None = None
        """Where datagrams go: the TCP peer IP, at the hub's UDP port from `welcome`."""
        self._hub_host = ""
        self._event_seq = 0
        self._closed_streams: deque[int] = deque(maxlen=_CLOSED_STREAMS)
        self._task: asyncio.Task[None] | None = None
        self._answered_before = False
        """Whether the hub has answered any Connection yet."""
        self._connections_lost = 0
        """Connections that ended since boot, answered or not: `diag` reports it."""
        self._vanished: list[ConnectionSocket] = []
        """Given up on, but left open: closing one would tell the hub."""
        self._layers = StatusLayers(connecting=True)
        self._identify_timer: asyncio.TimerHandle | None = None
        """Ends `identify`, while it shows."""
        self._leds = {
            cap.id: SimLed(cap.params["count"], board.led_brightness_cap)
            for cap in manifest.capabilities
            if cap.kind == "led"
        }
        self._status_led = next(iter(self._leds.values()), None)
        """The LED the Status shows on: the first one."""
        self._speakers = {
            cap.id: self._speaker(cap.id, board.default_volume, output_latency_ms)
            for cap in manifest.capabilities
            if cap.kind == "speaker"
        }
        self._mics = {
            cap.id: SimMic(
                self._clock, self._send_frame, lambda: self.faults.mic_tx_stall, self._mic_changed
            )
            for cap in manifest.capabilities
            if cap.kind == "mic"
        }
        self._buttons = {
            cap.id: SimButton(self._button_edge(cap.id))
            for cap in manifest.capabilities
            if cap.kind == "button"
        }

    @property
    def device_id(self) -> str:
        return self.manifest.device_id

    @property
    def token(self) -> str | None:
        return self._tokens.get(self.device_id)

    @property
    def status(self) -> Status:
        """The Status the device shows now."""
        return self._layers.shown

    @property
    def udp_port(self) -> int:
        assert self._udp is not None, "the device isn't running"
        return cast(tuple[str, int], self._udp.get_extra_info("sockname"))[1]

    @property
    def speaker(self) -> SimSpeaker:
        """The speaker capability, on a Board with one."""
        [speaker] = self._speakers.values()
        return speaker

    @property
    def mic(self) -> SimMic:
        """The mic capability, on a Board with one."""
        [mic] = self._mics.values()
        return mic

    @property
    def button(self) -> SimButton:
        """The button capability, on a Board with one."""
        [button] = self._buttons.values()
        return button

    def lights(self, cap: str = "led") -> tuple[Light, ...]:
        """What each LED of the led capability `cap` shows now, the Status included."""
        led = self._leds[cap]
        return led.lights(self.status if led is self._status_led else None)

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        """Wait until `predicate()` holds. It is checked on every change of this device."""
        while not predicate():
            await self._changed.wait()

    async def start(self) -> None:
        # The UDP socket is bound before the first `hello`, which announces its port.
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Datagrams(self._datagram), local_addr=(self._udp_host, 0)
        )
        self._udp = transport
        self._task = asyncio.create_task(self._run(), name=f"sim device {self.instance}")

    async def close(self) -> None:
        """Power the device off."""
        if self.state == "closed":
            return
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._udp is not None:
            self._udp.close()
        if self._identify_timer is not None:
            self._identify_timer.cancel()
        self._idle()
        self._close_vanished()
        self._set_state("closed")
        self._on_close(self)

    def _changed_now(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _set_state(self, state: DeviceState) -> None:
        self.state = state
        self._layers.connecting = state == "connecting"
        self._layers.pending = state == "pending"
        self._changed_now()

    def _close_vanished(self) -> None:
        for connection in self._vanished:
            connection.close()
        self._vanished.clear()

    async def _run(self) -> None:
        hub: tuple[str, int] | None = None
        while True:
            try:
                if hub is None:
                    hub = await self._resolve()
                await self._connection(hub)
            except (OSError, WireError) as e:
                _log.info("%s: connection failed: %s", self.instance, e)
            self.faults.connection = None
            self.conn_id = None
            self._idle()
            self._set_state("connecting")
            retry = self._backoff.failed()
            await asyncio.sleep(retry.delay)
            if retry.resolve:
                hub = None

    async def _connection(self, hub: tuple[str, int]) -> None:
        """One Connection, from connect to drop."""
        connection = ConnectionSocket(*await asyncio.open_connection(*hub))
        try:
            self.faults.connection = connection
            self._conn = connection
            self._hub_host = cast(tuple[str, int], connection.raw.getpeername())[0]
            self._hub_ip = _ip(self._hub_host)
            self._event_seq = 0
            self._closed_streams.clear()
            connection.raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.send(Hello(self.token, self.udp_port, self.manifest))
            decoder = ControlDecoder()
            answered = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._dead_after
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        data = await connection.reader.read(65536)
                        # A fault that struck while the read was pending holds what it got.
                        await self.faults.readable(connection)
                except TimeoutError:
                    _log.info("%s: the hub went silent", self.instance)
                    return
                if not data:
                    break
                for message in decoder.feed(data):
                    deadline = loop.time() + self._dead_after
                    if isinstance(message, (Pending, Welcome)) and not answered:
                        answered = True
                        self._answered(connection)
                    self._receive(message, connection)
            decoder.close()
        finally:
            self._conn = None
            self._hub_ip = None
            self._hub_udp = None
            self._connections_lost += 1
            if connection.vanished:
                self._vanished.append(connection)
            else:
                connection.close()

    def _answered(self, connection: ConnectionSocket) -> None:
        """The hub answered this Connection's `hello`, with `pending` or `welcome`."""
        if self._answered_before:
            self.reconnects += 1
        self._answered_before = True
        # This hello superseded any Connection the device gave up on, so the hub has closed
        # those by now, and closing them tells it nothing.
        self._close_vanished()

    def _receive(self, message: Message, connection: ConnectionSocket) -> None:
        match message:
            case Ping():
                connection.send(Pong(message.t, self._clock()))
            case Pending():
                self._set_state("pending")
            case Welcome():
                if message.token is not None:
                    self._tokens.set(self.device_id, message.token)
                self.name = message.name
                self.conn_id = message.conn_id
                self._hub_udp = (self._hub_host, message.udp_port)
                self._backoff.welcomed()
                self._layers.hub = "no_agent"
                self._set_state("online")
            case Command() if self.state == "online":
                reply = self._command(message)
                if delay := self.faults.delay_replies_ms:
                    asyncio.get_running_loop().call_later(delay / 1000, connection.send, reply)
                else:
                    connection.send(reply)
                self._changed_now()
            case Error():
                self.last_error = message.code
                if message.code == "unauthorized":
                    # Wipe the token, so the next Connection shows up pending.
                    self._tokens.set(self.device_id, None)
                self._changed_now()
            case _:
                pass

    def _idle(self) -> None:
        """The Connection is over: every capability goes idle."""
        for led in self._leds.values():
            led.release()
        for speaker in self._speakers.values():
            speaker.cut()
        for mic in self._mics.values():
            mic.cut()
        self._layers.claimed = False

    def _emit(self, cap: str, name: str, data: JsonObject, *, mirror: bool = False) -> None:
        """Send an Event on the Connection, if it's up; with `mirror`, over UDP too, twice."""
        if self._conn is None or self.state != "online" or self.conn_id is None:
            return
        event = Event(self._event_seq, cap, name, self._clock(), data)
        self._event_seq += 1
        if mirror:
            self._mirror(event, self.conn_id)
            asyncio.get_running_loop().call_later(
                MIRROR_GAP_MS / 1000, self._mirror, event, self.conn_id
            )
        self._conn.send_later(event, self.faults.delay_events_ms / 1000)

    def _mirror(self, event: Event, conn_id: int) -> None:
        """Send an Event's UDP mirror, if its Connection is still up."""
        if self.conn_id == conn_id:
            self._send_datagram(encode_datagram(EventMirror(conn_id, event)))

    # The button.

    def _button_edge(self, cap: str) -> Callable[[Edge], None]:
        return lambda edge: self._emit(cap, edge, {}, mirror=True)

    # UDP.

    def _datagram(self, data: bytes, source: tuple[str, int]) -> None:
        # The network's faults strike before the device sees the datagram.
        self.faults.udp_down.apply(data, lambda data: self._deliver(data, source))

    def _deliver(self, data: bytes, source: tuple[str, int]) -> None:
        """Take a datagram in, or drop it: UDP junk never closes a Connection."""
        if self._hub_ip is None or _ip(source[0]) != self._hub_ip:
            self.stats["bad_source"] += 1
            return
        try:
            datagram = decode_datagram(data)
        except WireError as e:
            self.stats[e.code] += 1
            return
        if not isinstance(datagram, StreamFrame):
            return  # an unknown kind, or an event mirror: the hub sends none
        if datagram.conn_id != self.conn_id:
            self.stats["bad_conn_id"] += 1
            return
        speaker = next((s for s in self._speakers.values() if s.stream == datagram.stream), None)
        if speaker is None:
            if datagram.stream not in self._closed_streams:
                self.stats["unknown_stream"] += 1
            return
        try:
            speaker.push(datagram.seq, datagram.payload)
        except WireError as e:
            self.stats[e.code] += 1

    def _send_frame(self, stream: int, seq: int, ts: int, payload: bytes) -> None:
        """Send a stream frame to the hub, through the network's faults."""
        if self.conn_id is not None:
            self._send_datagram(
                encode_datagram(StreamFrame(self.conn_id, stream, seq, ts, payload))
            )

    def _send_datagram(self, datagram: bytes) -> None:
        """Send a datagram to the hub, through the network's faults."""
        if self._udp is None or self._hub_udp is None:
            return
        udp, to = self._udp, self._hub_udp
        self.faults.udp_up.apply(datagram, lambda data: udp.sendto(data, to))

    # The mic.

    def _mic_changed(self) -> None:
        self._layers.listening = any(m.stream is not None for m in self._mics.values())
        self._changed_now()

    def _mic_tx_resumed(self) -> None:
        for mic in self._mics.values():
            mic.resume()

    def _mic_start(self, cap: Capability, args: JsonObject) -> JsonObject:
        """Raises `_Refused`."""
        mic = self._mics[cap.id]
        if mic.stream is not None:
            raise _Refused("busy", "the mic is streaming")
        self._check_exclusive(cap.id)
        format = AudioFormat.from_json(args["format"])
        if not format.offered(cap.params):
            raise _Refused("invalid_args", f"the mic doesn't capture {args['format']}")
        mic.start(args["stream"], format)
        return {"format": format.to_json()}

    def _check_exclusive(self, cap: str) -> None:
        """Raises `_Refused`: `busy` if a peer of `cap` in an exclusive group is active."""
        for group in self.manifest.exclusive:
            if cap in group:
                for peer in group:
                    if peer != cap and self._active(peer):
                        raise _Refused("busy", f"its exclusive peer {peer!r} is active")

    def _active(self, cap: str) -> bool:
        if (speaker := self._speakers.get(cap)) is not None and speaker.stream is not None:
            return True
        return (mic := self._mics.get(cap)) is not None and mic.stream is not None

    # The speaker.

    def _speaker(self, cap: str, level: int, output_latency_ms: int) -> SimSpeaker:
        def drained(stream: int, stats: JsonObject) -> None:
            self._closed_streams.append(stream)
            self._emit(cap, "drained", {"stream": stream} | stats)

        return SimSpeaker(level, output_latency_ms, drained, self._speaker_changed)

    def _speaker_changed(self) -> None:
        self._layers.speaking = any(s.stream is not None for s in self._speakers.values())
        self._changed_now()

    def _speaker_start(self, cap: Capability, args: JsonObject) -> JsonObject:
        """Raises `_Refused`."""
        speaker = self._speakers[cap.id]
        if speaker.stream is not None:
            raise _Refused("busy", "the speaker is playing")
        self._check_exclusive(cap.id)
        stream: int = args["stream"]
        if not 1 <= stream <= 127:
            raise _Refused("invalid_args", f"stream {stream} isn't a hub-range id")
        format = AudioFormat.from_json(args["format"])
        if not format.offered(cap.params):
            raise _Refused("invalid_args", f"the speaker doesn't play {args['format']}")
        speaker.start(stream, format, args.get("prefill_ms", 40))
        return {"format": format.to_json()}

    def _speaker_stop(self, cap: str) -> JsonObject:
        speaker = self._speakers[cap]
        if (stream := speaker.stream) is not None:
            self._closed_streams.append(stream)
        return speaker.stop()

    def _command(self, command: Command) -> Reply | FailedReply:
        if (code := self.faults.next_failure()) is not None:
            return FailedReply(command.id, code, "the fail_next fault")
        try:
            result = self._apply(command)
        except SchemaError as e:
            return FailedReply(command.id, "invalid_args", str(e))
        except _Refused as e:
            return FailedReply(command.id, e.code, e.message)
        return Reply(command.id, result)

    def _apply(self, command: Command) -> JsonObject:
        """Raises `SchemaError` or `_Refused`."""
        name, args = command.name, command.args
        cap = None
        if command.cap is None:
            contract = DEVICE_COMMANDS.get(name)
            kind = "device"
        else:
            cap = next((c for c in self.manifest.capabilities if c.id == command.cap), None)
            if cap is None:
                raise _Refused("unsupported", f"no capability {command.cap!r}")
            if cap.kind not in KINDS:
                raise _Refused("unsupported", f"{cap.id!r} is of an unknown Kind, {cap.kind!r}")
            contract = KINDS[cap.kind].commands.get(name)
            kind = cap.kind
        if contract is None:
            raise _Refused("unsupported", f"{kind} has no command {name!r}")
        validate(contract.args, args, "args")
        result: JsonObject = {}
        match kind, name:
            case "device", "status":
                self._layers.hub = args["state"]
            case "device", "identify":
                self._identify(seconds=args.get("s", 5))
            case "device", "diag":
                result = self._diag()
            case "speaker", "set_volume":
                assert command.cap is not None
                self._speakers[command.cap].level = args["level"]
            case "speaker", "start":
                assert cap is not None
                result = self._speaker_start(cap, args)
            case "speaker", "stop":
                assert command.cap is not None
                result = self._speaker_stop(command.cap)
            case "mic", "start":
                assert cap is not None
                result = self._mic_start(cap, args)
            case "mic", "stop":
                assert command.cap is not None
                result = self._mics[command.cap].stop()
            case "speaker", "drain":
                assert command.cap is not None
                self._speakers[command.cap].drain(args["last_seq"])
            case "led", "set":
                assert command.cap is not None
                try:
                    self._leds[command.cap].set(args)
                except ValueError as e:
                    raise _Refused("invalid_args", str(e)) from None
            case "led", "off":
                assert command.cap is not None
                self._leds[command.cap].off()
            case "led", "release":
                assert command.cap is not None
                self._leds[command.cap].release()
            case _:
                raise _Refused("unsupported", f"the Simulator doesn't do {kind} {name!r} yet")
        self._layers.claimed = self._status_led is not None and self._status_led.claim is not None
        return result

    def _identify(self, seconds: int) -> None:
        """Show `identify` for `seconds` from now, cutting short any earlier one."""
        if self._identify_timer is not None:
            self._identify_timer.cancel()
        self._layers.identify = True
        self._identify_timer = asyncio.get_running_loop().call_later(seconds, self._identified)

    def _identified(self) -> None:
        self._identify_timer = None
        self._layers.identify = False
        self._changed_now()

    def _diag(self) -> JsonObject:
        health = self._board.health
        return {
            "uptime_s": self._clock() // 1_000_000,
            "reset_reason": "poweron",
            "heap_free": health.heap_free,
            "heap_min": health.heap_min,
            "rssi": health.rssi,
            "reconnects": self._connections_lost,
            "stack_hwm": dict(health.stack_hwm),
        }

    def _clock(self) -> int:
        """The device clock: µs since boot."""
        return (time.monotonic_ns() - self._boot_ns) // 1000


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, received: Callable[[bytes, tuple[str, int]], None]) -> None:
        self._received = received

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._received(data, addr)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """An IPv4-mapped IPv6 address as the IPv4 one, so both socket families compare."""
    ip = ipaddress.ip_address(host.partition("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


class _Refused(Exception):
    """The device refuses a command with an error `reply`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


async def _static(address: tuple[str, int]) -> tuple[str, int]:
    return address


def _address(hub: str | tuple[str, int]) -> tuple[str, int]:
    if isinstance(hub, tuple):
        return hub
    host, _, port = hub.rpartition(":")
    return host.strip("[]"), int(port)


class _Tokens:
    """Device tokens by `device_id`: in memory, and in `<state_dir>/tokens.json` if given."""

    def __init__(self, state_dir: Path | None) -> None:
        self._path = None if state_dir is None else state_dir / "tokens.json"
        self._tokens: dict[str, str] = {}
        if self._path is not None and self._path.exists():
            self._tokens = cast(dict[str, str], json.loads(self._path.read_text()))

    def get(self, device_id: str) -> str | None:
        return self._tokens.get(device_id)

    def set(self, device_id: str, token: str | None) -> None:
        if token is None:
            self._tokens.pop(device_id, None)
        else:
            self._tokens[device_id] = token
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(json.dumps(self._tokens))
            os.replace(tmp, self._path)
