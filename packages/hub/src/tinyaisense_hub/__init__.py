from importlib.metadata import version

from .config import Config, ConfigError, EngineConfig, SpeechConfig, Timing, VoiceConfig
from .errors import HubError, HubErrorCode
from .hub import Hub
from .owner import OwnerClient
from .state import StateError

__version__ = version("tinyaisense-hub")

__all__ = [
    "Config",
    "ConfigError",
    "EngineConfig",
    "Hub",
    "HubError",
    "HubErrorCode",
    "OwnerClient",
    "SpeechConfig",
    "StateError",
    "Timing",
    "VoiceConfig",
]
