"""The agent API's binary frames: one audio frame per binary WebSocket message.

docs/protocol/agent.md#binary-frames. Integers are little-endian, and PCM is s16le mono.
"""

import struct
from dataclasses import dataclass

from .errors import WireError

_TO_HUB = struct.Struct("<H")
_TO_AGENT = struct.Struct("<HI")


@dataclass(frozen=True, slots=True)
class AgentAudio:
    """Audio an agent sends the hub: PCM of any even length, which the hub re-chunks."""

    handle: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class HubAudio:
    """Audio the hub sends an agent: one 10 ms frame, `index` frames into its stream."""

    handle: int
    index: int
    pcm: bytes


def decode_agent_audio(data: bytes) -> AgentAudio:
    """Raises `WireError`: `truncated` or `partial_sample`."""
    (handle,) = _unpack(_TO_HUB, data)
    return AgentAudio(handle, _pcm(data[_TO_HUB.size :]))


def encode_agent_audio(audio: AgentAudio) -> bytes:
    return _TO_HUB.pack(audio.handle) + audio.pcm


def decode_hub_audio(data: bytes) -> HubAudio:
    """Raises `WireError`: `truncated` or `partial_sample`."""
    handle, index = _unpack(_TO_AGENT, data)
    return HubAudio(handle, index, _pcm(data[_TO_AGENT.size :]))


def encode_hub_audio(audio: HubAudio) -> bytes:
    return _TO_AGENT.pack(audio.handle, audio.index) + audio.pcm


def _unpack(header: struct.Struct, data: bytes) -> tuple[int, ...]:
    if len(data) < header.size:
        raise WireError("truncated", f"{len(data)} bytes, the header is {header.size}")
    return header.unpack_from(data)


def _pcm(pcm: bytes) -> bytes:
    if len(pcm) % 2:
        raise WireError(
            "partial_sample", f"{len(pcm)} bytes of PCM isn't a whole number of samples"
        )
    return pcm
