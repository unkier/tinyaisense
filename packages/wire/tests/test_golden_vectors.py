"""Every golden vector in docs/protocol/vectors must pass through the codec's public API."""

from __future__ import annotations

import runpy
import typing
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from tinyaisense_wire import (
    AgentAudio,
    AudioFormat,
    ControlDecoder,
    EventMirror,
    HubAudio,
    Message,
    StreamFrame,
    WireError,
    WireErrorCode,
    decode_agent_audio,
    decode_datagram,
    decode_hub_audio,
    encode_agent_audio,
    encode_control,
    encode_datagram,
    encode_hub_audio,
)

VECTOR_DIR = Path(__file__).resolve().parents[3] / "docs" / "protocol" / "vectors"
VECTOR_FILE = VECTOR_DIR / "device.vectors"
AGENT_VECTOR_FILE = VECTOR_DIR / "agent.vectors"


@dataclass
class GoldenVector:
    name: str
    layer: str = ""
    expect: str = ""
    data: bytes = b""
    canon: bytes | None = None
    params: dict[str, int] = field(default_factory=dict[str, int])


def load_vectors(path: Path) -> list[GoldenVector]:
    vectors: list[GoldenVector] = []
    current: GoldenVector | None = None
    for raw in path.read_text(encoding="ascii").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        if keyword == "vector":
            current = GoldenVector(rest)
            continue
        assert current is not None, f"{keyword!r} outside a vector block"
        match keyword:
            case "layer":
                current.layer = rest
            case "expect":
                current.expect = rest
            case "param":
                key, value = rest.split()
                current.params[key] = int(value)
            case "hex":
                current.data += bytes.fromhex(rest)
            case "canon":
                current.canon = (current.canon or b"") + bytes.fromhex(rest)
            case "end":
                vectors.append(current)
                current = None
            case _:
                raise AssertionError(f"unknown statement {keyword!r}")
    return vectors


VECTORS = load_vectors(VECTOR_FILE)
AGENT_VECTORS = load_vectors(AGENT_VECTOR_FILE)


POSITIVE = [v for v in VECTORS + AGENT_VECTORS if v.expect == "ok"]
IGNORED = [v for v in VECTORS if v.expect == "ignore"]
NEGATIVE = [v for v in VECTORS + AGENT_VECTORS if v.expect not in ("ok", "ignore")]


def ids(v: GoldenVector) -> str:
    return v.name


def decode_tcp(data: bytes, *, chunk: int | None = None) -> list[Message]:
    decoder = ControlDecoder()
    step = chunk or max(len(data), 1)
    messages: list[Message] = []
    for i in range(0, len(data), step):
        messages += decoder.feed(data[i : i + step])
    decoder.close()
    return messages


def decode_udp(v: GoldenVector) -> StreamFrame | EventMirror | None:
    datagram = decode_datagram(v.data)
    if isinstance(datagram, StreamFrame) and "final" in v.params:
        fmt = AudioFormat("pcm_s16le", v.params["rate"], v.params["channels"], v.params["frame_ms"])
        fmt.check_frame(datagram.payload, final=bool(v.params["final"]))
    return datagram


def decode(
    v: GoldenVector,
) -> list[Message] | StreamFrame | EventMirror | AgentAudio | HubAudio | None:
    match v.layer:
        case "tcp":
            return decode_tcp(v.data)
        case "udp":
            return decode_udp(v)
        case "agent_to_hub":
            return decode_agent_audio(v.data)
        case "hub_to_agent":
            return decode_hub_audio(v.data)
        case _:
            raise AssertionError(f"unknown layer {v.layer!r}")


def reencode(v: GoldenVector) -> bytes:
    match decode(v):
        case list() as messages:
            return b"".join(encode_control(message) for message in messages)
        case AgentAudio() as audio:
            return encode_agent_audio(audio)
        case HubAudio() as audio:
            return encode_hub_audio(audio)
        case None:
            raise AssertionError("nothing decoded")
        case datagram:
            return encode_datagram(datagram)


@pytest.mark.parametrize("vector", POSITIVE, ids=ids)
def test_positive_vector_round_trips_byte_identically(vector: GoldenVector) -> None:
    assert reencode(vector) == (vector.data if vector.canon is None else vector.canon)


@pytest.mark.parametrize("vector", [v for v in POSITIVE if v.canon is not None], ids=ids)
def test_canonical_form_round_trips_byte_identically(vector: GoldenVector) -> None:
    assert vector.canon is not None
    assert reencode(replace(vector, data=vector.canon, canon=None)) == vector.canon


@pytest.mark.parametrize("vector", IGNORED, ids=ids)
def test_ignored_vector_decodes_to_nothing(vector: GoldenVector) -> None:
    assert decode(vector) in ([], None)


@pytest.mark.parametrize("vector", NEGATIVE, ids=ids)
def test_negative_vector_fails_with_its_documented_code(vector: GoldenVector) -> None:
    with pytest.raises(WireError) as failure:
        decode(vector)
    assert failure.value.code == vector.expect


@pytest.mark.parametrize("vector", [v for v in POSITIVE + IGNORED if v.layer == "tcp"], ids=ids)
def test_tcp_decoding_does_not_depend_on_how_the_stream_is_split(vector: GoldenVector) -> None:
    assert decode_tcp(vector.data, chunk=1) == decode_tcp(vector.data)


def test_every_decode_error_code_has_a_negative_device_vector() -> None:
    device_codes = {v.expect for v in NEGATIVE if v.layer in ("tcp", "udp")}
    assert device_codes == set(typing.get_args(WireErrorCode))


def test_committed_vectors_match_the_generator() -> None:
    generator = runpy.run_path(str(VECTOR_DIR / "generate.py"))
    assert generator["render"](generator["VECTORS"]) == VECTOR_FILE.read_text(encoding="ascii")
    assert generator["render"](
        generator["AGENT_VECTORS"], generator["AGENT_HEADER"]
    ) == AGENT_VECTOR_FILE.read_text(encoding="ascii")
