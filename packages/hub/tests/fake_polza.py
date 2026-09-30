"""A fake polza.ai, for tests: its audio API as polza.ai's docs and replies show it, with the
fake speech plugin's tones for speech (https://polza.ai/docs/api-reference/audio).

Its TTS speaks text as `fake.encode` does, at 24 kHz and in its own register, so that tests can
hear whether it or a local engine spoke. It sends a WAV whose sizes are all 0xFFFFFFFF, as
polza.ai does. Its STT hears the tones back. Any call can be made to fail, or to wait.
"""

import base64
import json
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tinyaisense_hub.speech import fake

RATE = 24000
"""Of its TTS's audio."""
REGISTER = 2
"""Its TTS speaks in this register."""
KEY = "polza-key"
"""The API key it takes."""


@dataclass(frozen=True, slots=True)
class Request:
    """A request it got."""

    path: str
    authorization: str | None
    body: dict[str, Any]


@dataclass(eq=False)
class FakePolza:
    """The fake, and how it answers: set these as a test goes."""

    url: str = ""
    """Its API's base URL, once it's serving."""
    requests: list[Request] = field(default_factory=list[Request])
    """Every request it got, in order."""
    status: int = 200
    """What it answers with: an error but for 200."""
    delay: float = 0.0
    """How long it takes to answer, in seconds."""
    content_type: str = "audio/wav"
    """Of the audio its TTS sends."""
    rate: int = RATE
    """Of the audio its TTS sends."""


def wav(pcm: bytes, rate: int) -> bytes:
    """s16le mono `pcm` in a WAV whose sizes are all 0xFFFFFFFF, as polza.ai sends it."""
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    unknown = 0xFFFFFFFF
    return (
        struct.pack("<4sI4s", b"RIFF", unknown, b"WAVE")
        + struct.pack("<4sI", b"fmt ", len(fmt))
        + fmt
        + struct.pack("<4sI", b"data", unknown)
        + pcm
    )


def pcm_of(data_uri: str) -> tuple[bytes, int]:
    """The audio of a `data:audio/wav;base64,` URI, and its rate. Its WAV is a plain one."""
    prefix = "data:audio/wav;base64,"
    assert data_uri.startswith(prefix)
    data = base64.b64decode(data_uri.removeprefix(prefix))
    riff, _, wave = struct.unpack_from("<4sI4s", data)
    assert (riff, wave) == (b"RIFF", b"WAVE")
    fmt, size = struct.unpack_from("<4sI", data, 12)
    assert fmt == b"fmt "
    tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, 20)
    assert (tag, channels, bits) == (1, 1, 16)
    chunk, length = struct.unpack_from("<4sI", data, 20 + size)
    assert chunk == b"data"
    pcm = data[28 + size :]
    assert len(pcm) == length
    return pcm, rate


def serve(polza: FakePolza) -> Iterator[FakePolza]:
    """Serve `polza` on loopback, until the generator is closed."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            polza.requests.append(Request(self.path, self.headers["Authorization"], body))
            time.sleep(polza.delay)
            if self.headers["Authorization"] != f"Bearer {KEY}":
                self._error(401, "UNAUTHORIZED", "Неверный ключ")
            elif polza.status != 200:
                message = "Все провайдеры вернули ошибку"  # noqa: RUF001
                self._error(polza.status, "SERVICE_UNAVAILABLE", message)
            elif self.path == "/api/v1/audio/speech":
                self._speech(body)
            elif self.path == "/api/v1/audio/transcriptions":
                self._transcription(body)
            else:
                self._error(404, "NOT_FOUND", "Ресурс не найден")  # noqa: RUF001

        def _speech(self, body: dict[str, Any]) -> None:
            audio = wav(fake.encode(body["input"], polza.rate, REGISTER), polza.rate)
            self._reply(
                200,
                {
                    "audio": base64.b64encode(audio).decode(),
                    "contentType": polza.content_type,
                    "model": body["model"].split("/")[-1],
                    "usage": {"characters": len(body["input"])},
                },
            )

        def _transcription(self, body: dict[str, Any]) -> None:
            pcm, rate = pcm_of(body["file"])
            self._reply(200, {"text": fake.decode(pcm, rate).text.replace(fake.SILENCE, "")})

        def _error(self, status: int, code: str, message: str) -> None:
            self._reply(status, {"error": {"code": code, "message": message}})

        def _reply(self, status: int, reply: dict[str, Any]) -> None:
            payload = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    polza.url = f"http://127.0.0.1:{server.server_address[1]}/api/v1"
    try:
        yield polza
    finally:
        server.shutdown()
        server.server_close()
