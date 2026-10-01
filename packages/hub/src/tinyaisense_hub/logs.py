"""The hub's log: stdlib logging, written as JSON lines.

A log call can carry fields for its line besides the message: `_log.info(..., extra=fields(
type="stream.ended", ...))`. Each line is one JSON object: `t`, `level`, `logger`, `msg`,
then the fields, then `exc` if there's a traceback.
"""

import json
import logging
import sys
from datetime import datetime
from types import TracebackType
from typing import Any


def fields(**values: Any) -> dict[str, Any]:
    """The `extra` of a log call whose line carries `values` besides the message."""
    return {"fields": values}


class JsonFormatter(logging.Formatter):
    """A record as one JSON line. `t` is the local time, ISO 8601 to the ms."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "t": datetime.fromtimestamp(record.created).astimezone().isoformat("T", "milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra: dict[str, Any] = getattr(record, "fields", {})
        line |= {key: value for key, value in extra.items() if key not in line}
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, ensure_ascii=False, default=str)


def setup() -> None:
    """Log INFO and up to stderr, as JSON lines: Python's warnings and an uncaught exception
    included. Does nothing if the root logger has a handler already."""
    if logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.captureWarnings(True)
    sys.excepthook = _uncaught


def _uncaught(
    kind: type[BaseException], value: BaseException, traceback: TracebackType | None
) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, traceback)
    else:
        logging.getLogger("tinyaisense_hub").critical(
            "uncaught exception", exc_info=(kind, value, traceback)
        )
