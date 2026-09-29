"""The hub config (TOML, read at start) and where the hub keeps its files."""

import os
import secrets
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, cast

from tinyaisense_wire import DEVICE_PORT, HTTP_PORT

APP = "tinyaisense"


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Timing:
    """Every hub timeout, in ms: the `[timing]` section."""

    hello_timeout_ms: int = 5000
    """A device or an agent link must send `hello` this soon after it connects."""
    ping_interval_ms: int = 2000
    """The hub pings each device this often, from its `hello` on."""
    dead_after_ms: int = 6000
    """A Connection with no inbound frame for this long is dead. More than the ping interval."""
    clock_window_ms: int = 20000
    """The device-clock offset comes from the shortest round trip among the pongs this recent."""
    agent_ping_interval_ms: int = 2000
    """The hub pings each agent link this often."""
    agent_dead_after_ms: int = 6000
    """An agent link with no inbound frame for this long is dead, and its Session ends."""
    speaker_buffer_ms: int = 300_000
    """How far ahead of playback an agent may send speaker audio. Past it, the stream ends
    with `overflow`."""
    mic_tail_ms: int = 30
    """After a mic's `stop` reply, how long its missing tail frames get to arrive before
    they're zero-filled."""
    agent_audio_ms: int = 2000
    """How much audio may wait for an agent's socket. Past it, the oldest frames are
    skipped."""


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """A speech engine: a Python class by import path, `module:Class`, and the keyword
    arguments to make it with."""

    cls: str
    params: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """A Voice: what agents pick by `id` to speak in `lang`."""

    id: str
    lang: str
    name: str
    engine_voice: str = ""
    """The TTS engine's own name for the voice: the `id` if left empty."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "engine_voice", self.engine_voice or self.id)


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    """The `[speech]` section: the engines, which role and language each serves, and the
    Voices."""

    engines: dict[str, EngineConfig] = field(default_factory=dict[str, EngineConfig])
    """By a name of the owner's choosing, which the roles refer to."""
    tts: dict[str, str] = field(default_factory=dict[str, str])
    """Language → the engine that speaks it."""
    voices: tuple[VoiceConfig, ...] = ()
    """The first Voice of each language is its default."""
    lang: str = "en"
    """The hub's default language, for text that doesn't show its own."""
    say_ahead_ms: int = 3000
    """How far ahead of playback a `say` is synthesized."""
    speaker_latency_ms: int = 50
    """How long after its schedule audio plays on a device: a `say` followed by another ends
    this long after its last frame was due."""

    def check(self) -> None:
        """Raises `ConfigError` if the engines, languages and Voices don't hold together."""
        for lang, name in self.tts.items():
            if name not in self.engines:
                raise ConfigError(f"speech.tts.{lang}: no engine {name!r} in [speech.engines]")
        ids: set[str] = set()
        for voice in self.voices:
            if voice.id in ids:
                raise ConfigError(f"two Voices have the id {voice.id!r}")
            ids.add(voice.id)
            if voice.lang not in self.tts:
                raise ConfigError(
                    f"Voice {voice.id!r} speaks {voice.lang}, which no engine in [speech.tts] "
                    "speaks"
                )
        spoken = {voice.lang for voice in self.voices}
        for lang in self.tts:
            if lang not in spoken:
                raise ConfigError(f"speech.tts.{lang}: no Voice speaks {lang}")
        if self.tts and self.lang not in spoken:
            raise ConfigError(f"speech.lang: no Voice speaks {self.lang}")


@dataclass(frozen=True, slots=True)
class Config:
    agent_token: str
    device_port: int = DEVICE_PORT
    """TCP and UDP. 0 picks free ports."""
    http_port: int = HTTP_PORT
    timing: Timing = field(default_factory=Timing)
    speech: SpeechConfig = field(default_factory=SpeechConfig)


def new_config() -> Config:
    return Config(agent_token=secrets.token_urlsafe(32))


def render_config(config: Config) -> str:
    timing = "\n".join(f"{f.name} = {getattr(config.timing, f.name)}" for f in fields(Timing))
    speech = config.speech
    return f"""\
# tinyaisense hub config. The hub reads it at start: restart the hub to apply changes.

# Agents authenticate with this token.
agent_token = "{config.agent_token}"

[ports]
# Device protocol, TCP and UDP.
device = {config.device_port}
# HTTP: /agent and /mcp.
http = {config.http_port}

# Every hub timeout, in ms.
[timing]
{timing}

# Speech: the engines, and the Voices agents speak in. With no TTS engine, `say` is
# unsupported.
[speech]
# The language of text that doesn't show its own: en or ru.
lang = "{speech.lang}"
# How far ahead of playback a `say` is synthesized, in ms.
say_ahead_ms = {speech.say_ahead_ms}
# How long after the hub's schedule audio plays on a device, in ms.
speaker_latency_ms = {speech.speaker_latency_ms}

# Each engine is a Python class, `module:Class`, and the keyword arguments to make it with,
# under a name of your choosing. For example:
#
# [speech.engines.my_tts]
# class = "my_plugin:MyTts"
# params = {{ model = "/path/to/model" }}
#
# The engine that speaks each language:
#
# [speech.tts]
# en = "my_tts"
#
# The Voices: agents pick one by `id`. The first of each language is its default.
#
# [[speech.voices]]
# id = "amy"
# lang = "en"
# name = "Amy"
# engine_voice = "en_US-amy"  # the engine's name for it; the id if left out
"""


def parse_config(text: str) -> Config:
    """Raises `ConfigError`."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from None
    top = _Table(doc, "")
    ports = top.table("ports")
    timing = top.table("timing")
    config = Config(
        agent_token=top.string("agent_token"),
        device_port=ports.port("device", DEVICE_PORT),
        http_port=ports.port("http", HTTP_PORT),
        timing=Timing(
            **{f.name: timing.positive_int(f.name, cast(int, f.default)) for f in fields(Timing)}
        ),
        speech=_speech(top.table("speech")),
    )
    for table in (top, ports, timing):
        table.check_unknown()
    if config.timing.dead_after_ms <= config.timing.ping_interval_ms:
        raise ConfigError("timing.dead_after_ms must be more than timing.ping_interval_ms")
    if config.timing.agent_dead_after_ms <= config.timing.agent_ping_interval_ms:
        raise ConfigError(
            "timing.agent_dead_after_ms must be more than timing.agent_ping_interval_ms"
        )
    return config


def _speech(table: _Table) -> SpeechConfig:
    engines = table.table("engines")
    tts = table.table("tts")
    defaults = SpeechConfig()
    speech = SpeechConfig(
        engines={name: _engine(engines.table(name)) for name in engines.names()},
        tts={lang: tts.string(lang) for lang in tts.names()},
        voices=tuple(_voice(voice) for voice in table.tables("voices")),
        lang=table.string("lang", defaults.lang),
        say_ahead_ms=table.positive_int("say_ahead_ms", defaults.say_ahead_ms),
        speaker_latency_ms=table.positive_int("speaker_latency_ms", defaults.speaker_latency_ms),
    )
    for checked in (table, engines, tts):
        checked.check_unknown()
    speech.check()
    return speech


def _engine(table: _Table) -> EngineConfig:
    engine = EngineConfig(table.string("class"), table.raw_table("params"))
    table.check_unknown()
    return engine


def _voice(table: _Table) -> VoiceConfig:
    voice = VoiceConfig(
        table.string("id"),
        table.string("lang"),
        table.string("name"),
        table.string("engine_voice", ""),
    )
    table.check_unknown()
    return voice


def load_config(path: Path) -> Config:
    """Raises `ConfigError`."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}: run `tinyaisense-hub init` first") from None
    try:
        return parse_config(text)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def write_config(path: Path, config: Config) -> None:
    """Write a new config, readable by the owner only. Raises `FileExistsError`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render_config(config))


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP / "hub.toml"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP


def runtime_dir() -> Path:
    base = _xdg("XDG_RUNTIME_DIR", None)
    if base is None:
        return Path(tempfile.gettempdir()) / f"{APP}-{os.getuid()}"
    return base / APP


def _xdg[T: Path | None](variable: str, default: T) -> Path | T:
    value = os.environ.get(variable, "")
    # The XDG spec says to ignore relative paths.
    return Path(value) if os.path.isabs(value) else default


class _Table:
    """Typed access to a TOML table. Raises `ConfigError`."""

    def __init__(self, table: dict[str, Any], where: str) -> None:
        self._table = table
        self._where = where
        self._read: set[str] = set()

    def _key(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def _get(self, key: str) -> object:
        self._read.add(key)
        return self._table.get(key)

    def table(self, key: str) -> _Table:
        value = self._get(key)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(f"[{self._key(key)}] must be a table")
        return _Table(cast(dict[str, Any], value), self._key(key))

    def tables(self, key: str) -> list[_Table]:
        """An array of tables, e.g. `[[speech.voices]]`."""
        value = self._get(key)
        if value is None:
            return []
        tables = cast(list[object], value) if isinstance(value, list) else None
        if tables is None or not all(isinstance(t, dict) for t in tables):
            raise ConfigError(f"[[{self._key(key)}]] must be an array of tables")
        tables = cast(list[dict[str, Any]], tables)
        return [_Table(t, f"{self._key(key)}[{i}]") for i, t in enumerate(tables)]

    def raw_table(self, key: str) -> dict[str, Any]:
        """A table of anything, e.g. an engine's `params`."""
        value = self._get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"{self._key(key)} must be a table")
        return cast(dict[str, Any], value)

    def names(self) -> list[str]:
        """Every key, each of which counts as known."""
        self._read.update(self._table)
        return list(self._table)

    def string(self, key: str, default: str | None = None) -> str:
        """A non-empty string, or `default` if it's left out and there's one."""
        value = self._get(key)
        if value is None and default is not None:
            return default
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{self._key(key)} must be a non-empty string")
        return value

    def port(self, key: str, default: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        if type(value) is not int or not 0 <= value <= 65535:
            raise ConfigError(f"{self._key(key)} must be a port number, 0..65535")
        return value

    def positive_int(self, key: str, default: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        if type(value) is not int or value <= 0:
            raise ConfigError(f"{self._key(key)} must be a positive integer")
        return value

    def check_unknown(self) -> None:
        if unknown := set(self._table) - self._read:
            raise ConfigError(f"unknown keys: {', '.join(sorted(map(self._key, unknown)))}")
