from importlib.metadata import version

from .session import ApiError, Notification, Session, SessionEnded, connect

__version__ = version("tinyaisense")

__all__ = ["ApiError", "Notification", "Session", "SessionEnded", "connect"]
