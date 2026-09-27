from importlib.metadata import version

from .faults import Faults
from .profiles import PROFILES
from .simulator import FIRMWARE, DeviceState, SimDevice, Simulator, device_id_for

__version__ = version("tinyaisense-sim")

__all__ = [
    "FIRMWARE",
    "PROFILES",
    "DeviceState",
    "Faults",
    "SimDevice",
    "Simulator",
    "device_id_for",
]
