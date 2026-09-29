"""The speech layer (ADR 0004): the engines, the Voices, and speaking text on speakers."""

import asyncio
import functools
import importlib
from concurrent.futures import ThreadPoolExecutor

from tinyaisense_wire import Capability, JsonObject

from ..config import ConfigError, SpeechConfig, VoiceConfig
from ..control import Control
from ..errors import HubError
from ..resampler import RATES
from ..session import Session
from ..state import DeviceRecord
from .engines import Engine, Tts
from .say import EngineVoice, Run, Say
from .text import IDLE_MS, Script, language


class Speech:
    """The engines, made and warmed up by `start`, and what the hub does with them."""

    def __init__(self, config: SpeechConfig, control: Control) -> None:
        self._config = config
        self._control = control
        self._executors: dict[type[Engine], ThreadPoolExecutor] = {}
        """Role → the one thread its engines run on."""
        self._tts: dict[str, Tts] = {}
        """Language → the engine that speaks it."""
        self._defaults: dict[str, VoiceConfig] = {}
        """Language → its default Voice."""
        self._runs: dict[tuple[str, str], Run] = {}
        """(device_id, capability) → the newest Run on the speaker, until it's over."""
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Make and warm up the engines, and check them against the config. Raises
        `ConfigError`."""
        self._config.check()
        try:
            await self._start_tts()
        except BaseException:
            self.close()
            raise

    async def _start_tts(self) -> None:
        engines: dict[str, Tts] = {}
        for lang, name in self._config.tts.items():
            if name not in engines:
                engines[name] = await self._load(name, Tts)
                if engines[name].rate not in RATES:
                    raise ConfigError(
                        f"speech engine {name!r} speaks at {engines[name].rate} Hz: the hub "
                        f"resamples from {', '.join(map(str, RATES))} Hz only"
                    )
            engine = self._tts[lang] = engines[name]
            if engine.languages and lang not in engine.languages:
                raise ConfigError(
                    f"speech engine {name!r} doesn't speak {lang}, only "
                    f"{', '.join(engine.languages)}"
                )
        for voice in self._config.voices:
            self._defaults.setdefault(voice.lang, voice)
            engine = self._tts[voice.lang]
            if engine.voices is not None and voice.engine_voice not in engine.voices:
                raise ConfigError(
                    f"Voice {voice.id!r}: speech engine {self._config.tts[voice.lang]!r} has no "
                    f"voice {voice.engine_voice!r}"
                )

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)

    def voices(self) -> list[JsonObject]:
        return [{"id": v.id, "lang": v.lang, "name": v.name} for v in self._config.voices]

    async def say(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        text: str,
        *,
        voice: str | None,
        lang: str | None,
        replace: bool = False,
    ) -> JsonObject:
        """Speak `text` on a speaker, after the `say`s queued on it, or cutting them off if
        `replace`. Returns how it went once it has played. Raises `HubError`."""
        say = self._say(session, device, capability, Script.whole(text), voice, lang)
        self._more(say)
        self._run(session, device, capability, replace).add(say)
        return await asyncio.shield(say.done)

    def say_open(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        *,
        voice: str | None,
        lang: str | None,
        replace: bool = False,
    ) -> int:
        """Start a `say` whose text comes in with `say_append`, queued as `say` would be.
        Returns its handle. Raises `HubError`; once it's open, the error its speech failed to
        start with comes from `say_append` and `say_close`."""
        say = self._say(session, device, capability, Script(), voice, lang)
        self._control.speaker_check(device, capability)
        handle = session.new_handle()
        session.says[handle] = say
        say.done.add_done_callback(_retrieve)  # in case it's never closed
        self._run(session, device, capability, replace).add(say)
        return handle

    def say_append(self, session: Session, handle: int, text: str) -> None:
        """More text for an open `say`: each clause it completes is spoken, and its tail is
        once no text comes for `IDLE_MS`. Once the `say` has ended, the text is ignored.
        Raises `HubError`."""
        say = self._open_say(session, handle)
        if say.done.done():
            if (e := say.done.exception()) is not None:
                raise e
            return
        say.script.append(text)
        self._more(say)
        if say.idle is not None:
            say.idle.cancel()
        say.idle = asyncio.get_running_loop().call_later(IDLE_MS / 1000, self._idle, say)

    async def say_close(self, session: Session, handle: int) -> JsonObject:
        """No more text comes for an open `say`: its tail is spoken. Returns how it went once
        it has played, or at once if it has ended. Raises `HubError`."""
        say = self._open_say(session, handle)
        del session.says[handle]
        if say.idle is not None:
            say.idle.cancel()
        if not say.done.done():
            say.script.close()
            self._more(say)
        return await asyncio.shield(say.done)

    def _say(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        script: Script,
        voice: str | None,
        lang: str | None,
    ) -> Say:
        """A new `say` of `script`, in `voice` or `lang` if given. Raises `HubError`."""
        if not self._tts:
            raise HubError("unsupported", "the hub has no TTS engine")
        say = Say(script, session, f"{device.name}/{capability.id}")
        if (chosen := self._voice(voice, lang)) is not None:
            say.voice = self._engine_voice(chosen)
        return say

    def _open_say(self, session: Session, handle: int) -> Say:
        """The open `say` `handle`. Raises `HubError`."""
        say = session.says.get(handle)
        if say is None:
            raise HubError("disconnected", f"no say {handle} is open")
        return say

    def _more(self, say: Say) -> None:
        """`say`'s script has more clauses, or has closed: have them spoken. Its Voice, if it
        has none yet, is the default of the language they're written in, or of the hub's
        default language: a streamed `say` is judged by the clauses it has when its first is
        cut."""
        if say.voice is None and say.script.clauses:
            written = language(" ".join(clause.text for clause in say.script.clauses))
            chosen = self._defaults.get(written or "", self._defaults[self._config.lang])
            say.voice = self._engine_voice(chosen)
        say.more.set()

    def _idle(self, say: Say) -> None:
        """No text has come for `say` for `IDLE_MS`."""
        if not say.done.done():
            say.script.idle()
            self._more(say)

    def _engine_voice(self, voice: VoiceConfig) -> EngineVoice:
        return EngineVoice(self._tts[voice.lang], voice.engine_voice)

    def _run(
        self, session: Session, device: DeviceRecord, capability: Capability, replace: bool
    ) -> Run:
        """The Run a `say` joins: the speaker's, or a new one after it, or cutting it off."""
        key = (device.device_id, capability.id)
        run = self._runs.get(key)
        after = None
        if run is not None and run.session is session:
            if replace:
                run.replace()
            elif run.accepting:
                return run
            elif run.draining:
                after = run
        run = self._runs[key] = Run(
            self._config, self._control, self._executors[Tts], session, device, capability, after
        )
        task = asyncio.create_task(run.speak())
        self._tasks.add(task)

        def done(task: asyncio.Task[None]) -> None:
            self._tasks.discard(task)
            if self._runs.get(key) is run:
                del self._runs[key]

        task.add_done_callback(done)
        return run

    def _voice(self, voice: str | None, lang: str | None) -> VoiceConfig | None:
        """The Voice a `say` speaks in: `voice`, else the default Voice of `lang`; None if
        neither is given. Raises `HubError`."""
        if voice is not None:
            chosen = next((v for v in self._config.voices if v.id == voice), None)
            if chosen is None:
                raise HubError("invalid_args", f"no voice {voice!r}: see voices")
            if lang is not None and lang != chosen.lang:
                raise HubError("invalid_args", f"voice {voice!r} speaks {chosen.lang}, not {lang}")
            return chosen
        if lang is not None:
            if lang not in self._defaults:
                raise HubError("unsupported", f"no voice speaks {lang!r}")
            return self._defaults[lang]
        return None

    async def _load[E: Engine](self, name: str, role: type[E]) -> E:
        """Make engine `name` on `role`'s thread, and warm it up there."""
        config = self._config.engines[name]
        module, _, qualname = config.cls.partition(":")
        try:
            cls = functools.reduce(getattr, qualname.split("."), importlib.import_module(module))
        except (ImportError, AttributeError) as e:
            raise ConfigError(f"speech engine {name!r}: can't import {config.cls}: {e}") from e
        if not isinstance(cls, type) or not issubclass(cls, role):
            raise ConfigError(f"speech engine {name!r}: {config.cls} isn't a {role.__name__}")
        executor = self._executors.get(role)
        if executor is None:
            executor = self._executors[role] = ThreadPoolExecutor(
                1, thread_name_prefix=f"speech-{role.__name__.lower()}"
            )
        loop = asyncio.get_running_loop()
        try:
            engine = await loop.run_in_executor(executor, functools.partial(cls, **config.params))
            await loop.run_in_executor(executor, engine.warm_up)
        except Exception as e:
            raise ConfigError(f"speech engine {name!r}: {e}") from e
        return engine


def _retrieve(done: asyncio.Future[JsonObject]) -> None:
    """Mark the error a `say` failed with as seen."""
    if not done.cancelled():
        done.exception()
