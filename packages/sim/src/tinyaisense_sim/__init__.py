from importlib.metadata import version

from .faults import Faults
from .led import Light
from .profiles import PROFILES, Board
from .simulator import FIRMWARE, DeviceState, SimDevice, Simulator, device_id_for
from .status import HubStatus, Status

__version__ = version("tinyaisense-sim")

__all__ = [
    "FIRMWARE",
    "PROFILES",
    "Board",
    "DeviceState",
    "Faults",
    "HubStatus",
    "Light",
    "SimDevice",
    "Simulator",
    "Status",
    "device_id_for",
]
