from importlib.metadata import version

from . import sinks, sources
from .button import SimButton
from .faults import Faults, GilbertElliott, UdpFaults
from .led import Light
from .mic import SimMic
from .profiles import PROFILES, Board, Health, from_manifest
from .simulator import FIRMWARE, DeviceState, SimDevice, Simulator, device_id_for
from .sinks import CapturedFrame
from .speaker import SimSpeaker
from .status import HubStatus, Status

__version__ = version("tinyaisense-sim")

__all__ = [
    "FIRMWARE",
    "PROFILES",
    "Board",
    "CapturedFrame",
    "DeviceState",
    "Faults",
    "GilbertElliott",
    "Health",
    "HubStatus",
    "Light",
    "SimButton",
    "SimDevice",
    "SimMic",
    "SimSpeaker",
    "Simulator",
    "Status",
    "UdpFaults",
    "device_id_for",
    "from_manifest",
    "sinks",
    "sources",
]
