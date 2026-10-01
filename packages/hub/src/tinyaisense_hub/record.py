"""The recording dir, `[debug] record_dir`: a dir per Session, with a WAV per stream and a
JSONL trace, for replay.

A Session's dir is `<local start time>-<session id>`. Its streams' WAVs are numbered in the
order they started, e.g. `001-kitchen-mic.wav`: 16 kHz mono PCM, as the mic's timeline (its
gaps zero-filled) or as the frames sent to the speaker. Each line of `trace.jsonl` is one
record, `{t, type, ...}`, with `t` in epoch µs:

- `session.started {session, holder, utterances}`, first;
- `call {call, via, method, params}`: an agent's call, by the agent API (`via: agent_api`) or
  an MCP tool (`via: mcp`), then `reply {call, result}` or `reply {call, error}` as it ends,
  or `reply {call, cancelled: true}` if it was cut off, unanswered, as its link closed;
- `notify {method, params}`: a notification to the agent;
- `stream.started {file, address, kind, stream, handle}`, and `stream.ended {file, reason,
  stats, ...}` with what the stream's log line carries;
- `session.ended {reason}`. Only the replies of the calls still running then follow it.

Everything is written synchronously, on the loop. Writes go through the OS's buffers, so the
cost is a copy. A recording that can't be written is logged, and given up.
"""

import json
import logging
import struct
import time
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from tinyaisense_wire import JsonObject

PATCH_EVERY = 100
"""How many writes a WAV's header may lag behind: every this many, about a second of a
stream, the sizes in it are brought up to date, so that a WAV the hub never closed plays up to
there."""

_RIFF = struct.Struct("<4sI4s4sIHHIIHH4sI")

_log = logging.getLogger("tinyaisense_hub.record")


class Recorder:
    """Records every Session under `root`."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def start(self) -> None:
        """Raises `OSError` if `root` can't be made."""
        self._root.mkdir(parents=True, exist_ok=True)

    def session(
        self, session: str, holder: str, since: int, utterances: JsonObject
    ) -> SessionRecording | None:
        """Start recording Session `session`, which started at `since`, in epoch ms. None if
        its dir or trace can't be made."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(since / 1000))
        path = self._root / f"{stamp}-{session}"
        try:
            path.mkdir()
            trace = (path / "trace.jsonl").open("x", buffering=1, encoding="utf-8")
        except OSError as e:
            _log.warning("not recording Session %s: %s", session, e)
            return None
        recording = SessionRecording(path, trace)
        recording.write("session.started", session=session, holder=holder, utterances=utterances)
        return recording


class SessionRecording:
    """One Session's dir: its trace, and its streams' WAVs."""

    def __init__(self, path: Path, trace: TextIO) -> None:
        self.path = path
        """Its dir."""
        self._trace: TextIO | None = trace
        self._ended = False
        """`session.ended` is in: only replies follow it."""
        self._streams = 0
        self._calls = 0
        self._running: set[int] = set()
        """The calls not yet replied to. The trace stays open, after the Session's end, until
        there are none."""

    def notify(self, method: str, params: JsonObject) -> None:
        """A notification to the agent."""
        if not self._ended:
            self.write("notify", method=method, params=params)

    def call(self, via: str, method: str, params: JsonObject) -> TracedCall:
        """An agent's call, which goes on until its `TracedCall` gets its reply."""
        if self._ended:
            return TracedCall(None, 0)
        self._calls += 1
        self._running.add(self._calls)
        self.write("call", call=self._calls, via=via, method=method, params=params)
        return TracedCall(self, self._calls)

    def stream(
        self, address: str, kind: str, stream: int, handle: int | None, rate: int
    ) -> StreamRecording | None:
        """Start a WAV for a stream of mono PCM at `rate`. None if it can't be made."""
        if self._ended:
            return None
        self._streams += 1
        name = f"{self._streams:03d}-{address.replace('/', '-')}.wav"
        try:
            file = (self.path / name).open("xb")  # open while the stream lasts
            file.write(_header(rate, 0))
        except OSError as e:
            _log.warning("%s: not recording %s: %s", self.path, address, e)
            return None
        self.write(
            "stream.started", file=name, address=address, kind=kind, stream=stream, handle=handle
        )
        return StreamRecording(self, name, file, rate)

    def close(self, reason: str) -> None:
        """The Session ended. The trace closes once the calls still running have ended too."""
        self.write("session.ended", reason=reason)
        self._ended = True
        if not self._running:
            self._close_trace()

    def reply(self, call: int, **fields: Any) -> None:
        """How call `call` ended."""
        self.write("reply", call=call, **fields)
        self._running.discard(call)
        if self._ended and not self._running:
            self._close_trace()

    def write(self, type: str, **fields: Any) -> None:
        """Add a record to the trace, stamped now."""
        if self._trace is None:
            return
        record = {"t": time.time_ns() // 1000, "type": type, **fields}
        try:
            self._trace.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as e:
            _log.warning("%s: the trace stops: %s", self.path, e)
            self._close_trace()

    def _close_trace(self) -> None:
        if (trace := self._trace) is not None:
            self._trace = None
            try:
                trace.close()
            except OSError as e:
                _log.warning("%s: the trace stops: %s", self.path, e)


class TracedCall:
    """An agent's call in the trace, until its reply. It traces nothing if its Session isn't
    recorded."""

    def __init__(self, session: SessionRecording | None, call: int) -> None:
        self._session = session
        self._call = call

    def replied(self, outcome: JsonObject, *, error: bool = False) -> None:
        """It ended: its result, or its `error`, `{code, message, ...}`."""
        self._reply({"error" if error else "result": outcome})

    def cancelled(self) -> None:
        """It was cut off, unanswered."""
        self._reply({"cancelled": True})

    def _reply(self, fields: JsonObject) -> None:
        if (session := self._session) is not None:
            self._session = None
            session.reply(self._call, **fields)


class StreamRecording:
    """One stream's WAV."""

    def __init__(self, session: SessionRecording, name: str, file: BinaryIO, rate: int) -> None:
        self._session = session
        self._name = name
        self._file: BinaryIO | None = file
        self._rate = rate
        self._size = 0
        self._writes = 0

    def write(self, pcm: bytes) -> None:
        if (file := self._file) is None:
            return
        try:
            file.write(pcm)
            self._size += len(pcm)
            self._writes += 1
            if self._writes % PATCH_EVERY == 0:
                self._patch(file)
        except OSError as e:
            _log.warning("%s: %s stops: %s", self._session.path, self._name, e)
            self._close()

    def close(self, end: JsonObject) -> None:
        """The stream ended: `end` is what its log line says of how."""
        self._close()
        self._session.write("stream.ended", file=self._name, **end)

    def _patch(self, file: BinaryIO) -> None:
        """Bring the header's sizes up to date."""
        file.seek(0)
        file.write(_header(self._rate, self._size))
        file.seek(0, 2)

    def _close(self) -> None:
        if (file := self._file) is not None:
            self._file = None
            try:
                with file:
                    self._patch(file)
            except OSError as e:
                _log.warning("%s: %s stops: %s", self._session.path, self._name, e)


def _header(rate: int, size: int) -> bytes:
    """The 44-byte header of a WAV of `size` bytes of mono s16le PCM at `rate`."""
    return _RIFF.pack(
        b"RIFF", 36 + size, b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", size
    )
