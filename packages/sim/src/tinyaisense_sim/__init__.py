from importlib.metadata import version

from . import sources
from .faults import Faults, GilbertElliott, UdpFaults
from .led import Light
from .mic import SimMic
from .profiles import PROFILES, Board, Health
from .simulator import FIRMWARE, DeviceState, SimDevice, Simulator, device_id_for
from .speaker import CapturedFrame, SimSpeaker
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
    "SimDevice",
    "SimMic",
    "SimSpeaker",
    "Simulator",
    "Status",
    "UdpFaults",
    "device_id_for",
    "sources",
]
