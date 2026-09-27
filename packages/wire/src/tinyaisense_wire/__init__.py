from importlib.metadata import version

from .audio import AudioFormat
from .control import ControlDecoder, encode_control
from .datagrams import EventMirror, StreamFrame, decode_datagram, encode_datagram
from .errors import WireError, WireErrorCode
from .frames import MAX_FRAME_BODY, Frame, FrameDecoder, FrameType, encode_frame
from .kinds import DEVICE_COMMANDS, KINDS, CommandContract, EventContract, KindContract, Schema
from .messages import (
    DEVICE_PORT,
    HTTP_PORT,
    MDNS_SERVICE,
    PROTOCOL,
    Capability,
    Command,
    Error,
    Event,
    FailedReply,
    Hello,
    JsonObject,
    Manifest,
    Message,
    Pending,
    Ping,
    Pong,
    Reply,
    Welcome,
    decode_message,
    encode_message,
)
from .schema import SchemaError, validate

__version__ = version("tinyaisense-wire")

__all__ = [
    "DEVICE_COMMANDS",
    "DEVICE_PORT",
    "HTTP_PORT",
    "KINDS",
    "MAX_FRAME_BODY",
    "MDNS_SERVICE",
    "PROTOCOL",
    "AudioFormat",
    "Capability",
    "Command",
    "CommandContract",
    "ControlDecoder",
    "Error",
    "Event",
    "EventContract",
    "EventMirror",
    "FailedReply",
    "Frame",
    "FrameDecoder",
    "FrameType",
    "Hello",
    "JsonObject",
    "KindContract",
    "Manifest",
    "Message",
    "Pending",
    "Ping",
    "Pong",
    "Reply",
    "Schema",
    "SchemaError",
    "StreamFrame",
    "Welcome",
    "WireError",
    "WireErrorCode",
    "decode_datagram",
    "decode_message",
    "encode_control",
    "encode_datagram",
    "encode_frame",
    "encode_message",
    "validate",
]
