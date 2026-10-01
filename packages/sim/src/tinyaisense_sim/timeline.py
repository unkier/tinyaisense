"""Each simulated device's timeline, and the JSONL trace of every device's."""

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tinyaisense_wire import JsonObject

TIMELINE_MAX = 10_000
"""A timeline keeps this many of its latest entries."""

_log = logging.getLogger("tinyaisense_sim.timeline")

type EntryType = Literal["status", "tx", "rx", "edge", "fault"]


@dataclass(frozen=True, slots=True)
class Entry:
    t: int
    """When, in µs since the epoch: the clock the hub's own trace uses."""
    type: EntryType
    """What happened, with `data`:
    - `status`: the device's `state` or the `status` it shows changed;
    - `tx`, `rx`: the control message `msg` went out or came in on the Connection;
    - `edge`: the button `cap` went `down` or `up`, whether or not it went out;
    - `fault`: the fault `fault` struck, with its arguments, or was set (`on`) or cleared. The
      plain settings, such as UDP loss or the delays, aren't noted.
    """
    data: JsonObject


class Trace:
    """A JSONL file of every device's timeline, an entry a line as it happens:
    `{"t", "device", "type", ...data}`."""

    def __init__(self, path: Path) -> None:
        self._file = path.open("w", encoding="utf-8")

    def write(self, device: str, entry: Entry) -> None:
        line = {"t": entry.t, "device": device, "type": entry.type} | entry.data
        self._file.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class Timeline:
    """One device's timeline: its latest `TIMELINE_MAX` entries, also written to the trace and
    logged at DEBUG."""

    def __init__(self, device: str, trace: Trace | None) -> None:
        self._device = device
        self._trace = trace
        self.entries: deque[Entry] = deque(maxlen=TIMELINE_MAX)

    def note(self, type: EntryType, data: JsonObject) -> None:
        entry = Entry(time.time_ns() // 1000, type, data)
        self.entries.append(entry)
        if self._trace is not None:
            self._trace.write(self._device, entry)
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug("%s %s %s", self._device, type, json.dumps(data, separators=(",", ":")))
