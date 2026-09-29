from importlib.metadata import version

from .faults import Faults
from .led import Light
from .profiles import PROFILES, Board, Health
from .simulator import FIRMWARE, DeviceState, SimDevice, Simulator, device_id_for
from .speaker import SimSpeaker
from .status import HubStatus, Status

__version__ = version("tinyaisense-sim")

__all__ = [
    "FIRMWARE",
    "PROFILES",
    "Board",
    "DeviceState",
    "Faults",
    "Health",
    "HubStatus",
    "Light",
    "SimDevice",
    "SimSpeaker",
    "Simulator",
    "Status",
    "device_id_for",
]
