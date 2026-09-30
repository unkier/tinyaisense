"""The polza.ai cloud speech engines: TTS and STT through its OpenAI-like audio API
(https://polza.ai/docs/api-reference/audio), one POST per clause or segment. They need the
`[polza]` extra, and the API key in env `POLZA_API_KEY`.

Name each with a local engine to stand in for it, e.g. `ru = ["polza_tts", "silero_tts"]` in
`[speech.tts]`: the hub falls back to that one when polza.ai fails or is slow.
"""

import base64
import os
import struct
from collections.abc import Iterator, Mapping
from typing import Any, cast

try:
    import httpx
except ImportError as e:
    raise ImportError(f"{e}: the polza.ai speech engines need tinyaisense-hub[polza]") from e

from .engines import Stt, Transcript, Tts
from .text import language

URL = "https://polza.ai/api/v1"
KEY_ENV = "POLZA_API_KEY"


class PolzaError(Exception):
    pass


class _Api:
    """polza.ai's API, over kept-alive HTTP connections."""

    def __init__(self, url: str, key_env: str, timeout: float) -> None:
        key = os.environ.get(key_env)
        if not key:
            raise PolzaError(f"polza.ai needs its API key in env {key_env}")
        self._client = httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout
        )

    def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Raises `PolzaError`."""
        try:
            reply = self._client.post(path, json=body)
        except httpx.TimeoutException:
            raise PolzaError("polza.ai: timed out") from None
        except httpx.HTTPError as e:
            raise PolzaError(f"polza.ai: {e}") from e
        try:
            data: object = reply.json()
        except ValueError:
            data = None
        if reply.status_code == 200 and isinstance(data, dict):
            return cast(dict[str, Any], data)
        match data:
            case {"error": {"code": str(code), "message": str(message)}}:
                detail = f"{code}: {message}"
            case _:
                detail = reply.text[:200]
        raise PolzaError(f"polza.ai: {reply.status_code} {detail}")


class PolzaTts(Tts):
    """polza.ai's TTS: OpenAI's voices, e.g. `alloy` or `nova`, or ElevenLabs'. It gives the
    audio of a text all at once, as a WAV."""

    cloud = True

    def __init__(
        self,
        *,
        model: str = "openai/tts-1",
        rate: int = 24000,
        options: Mapping[str, Any] | None = None,
        url: str = URL,
        key_env: str = KEY_ENV,
        timeout: float = 5.0,
    ) -> None:
        """`rate` is that of the audio `model` gives: OpenAI's is 24 kHz. `options` go in each
        request as they are, e.g. `speed`. A call fails after `timeout` s with no progress."""
        self.rate = rate
        self._model = model
        self._options = dict(options or {})
        self._api = _Api(url, key_env, timeout)

    def synthesize(self, text: str, voice: str) -> Iterator[bytes]:
        body = {"model": self._model, "input": text, "voice": voice, "response_format": "wav"}
        reply = self._api.post("/audio/speech", body | self._options)
        if (kind := reply.get("contentType")) != "audio/wav":
            raise PolzaError(f"polza.ai sent {kind}, not audio/wav")
        pcm, rate = _read_wav(base64.b64decode(reply["audio"]))
        if rate != self.rate:
            raise PolzaError(f"polza.ai sent {rate} Hz audio: set the engine's rate to it")
        yield pcm


class PolzaStt(Stt):
    """polza.ai's STT. The language of its transcript is the one asked for, else that of the
    script it's written in."""

    cloud = True
    rate = 16000

    def __init__(
        self,
        *,
        model: str = "openai/gpt-4o-mini-transcribe",
        options: Mapping[str, Any] | None = None,
        url: str = URL,
        key_env: str = KEY_ENV,
        timeout: float = 5.0,
    ) -> None:
        """`options` go in each request as they are, e.g. `prompt`. A call fails after
        `timeout` s with no progress."""
        self._model = model
        self._options = dict(options or {})
        self._api = _Api(url, key_env, timeout)

    def transcribe(self, pcm: bytes, lang: str | None) -> Transcript:
        audio = base64.b64encode(_wav(pcm, self.rate)).decode()
        body: dict[str, Any] = {"model": self._model, "file": f"data:audio/wav;base64,{audio}"}
        if lang is not None:
            body["language"] = lang
        text = str(self._api.post("/audio/transcriptions", body | self._options)["text"]).strip()
        return Transcript(text, lang or language(text) or "en")


def _wav(pcm: bytes, rate: int) -> bytes:
    """s16le mono `pcm` as a WAV."""
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        struct.pack("<4sI4s", b"RIFF", 4 + 8 + len(fmt) + 8 + len(pcm), b"WAVE")
        + struct.pack("<4sI", b"fmt ", len(fmt))
        + fmt
        + struct.pack("<4sI", b"data", len(pcm))
        + pcm
    )


_UNKNOWN = 0xFFFFFFFF
"""A chunk size a WAV made as it streams has: its data runs to the end."""


def _read_wav(data: bytes) -> tuple[bytes, int]:
    """The audio of an s16le mono WAV, and its rate. Raises `PolzaError`."""
    try:
        riff, _, wave = struct.unpack_from("<4sI4s", data)
        if (riff, wave) != (b"RIFF", b"WAVE"):
            raise PolzaError("polza.ai sent audio that isn't a WAV")
        at, rate = 12, None
        while True:
            chunk, size = struct.unpack_from("<4sI", data, at)
            at += 8
            if chunk == b"fmt ":
                tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, at)
                if (tag, channels, bits) != (1, 1, 16):
                    raise PolzaError("polza.ai sent audio that isn't 16-bit mono PCM")
            elif chunk == b"data":
                if rate is None:
                    raise PolzaError("polza.ai sent a WAV with no format")
                end = len(data) if size == _UNKNOWN else at + size
                return data[at:end], rate
            at += size + size % 2
    except struct.error:
        raise PolzaError("polza.ai sent a WAV that's cut short") from None
