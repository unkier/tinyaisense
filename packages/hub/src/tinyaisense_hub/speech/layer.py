"""The speech layer (ADR 0004): the engines, the Voices, and speaking text on speakers."""

import asyncio
import functools
import importlib
import logging
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tinyaisense_wire import Capability, JsonObject

from ..config import ConfigError, EngineNames, SpeechConfig, Timing, VoiceConfig, engine_names
from ..control import Control
from ..errors import HubError
from ..mic import Listener
from ..session import Session, ThenListen, UtteranceOptIn
from ..state import DeviceRecord
from ..streams import FORMAT
from .cloud import THREADS, Engines, Runner
from .engines import Engine, Stt, Tts, Turn, Vad
from .listen import Ears, Listen, Trigger
from .say import EngineVoice, Run, Say
from .text import IDLE_MS, Script, language

_log = logging.getLogger("tinyaisense_hub.speech")


class Speech:
    """The engines, made and warmed up by `start`, and what the hub does with them."""

    def __init__(self, config: SpeechConfig, control: Control, timing: Timing) -> None:
        self._config = config
        self._control = control
        self._timing = timing
        self._executors: dict[type[Engine] | str, ThreadPoolExecutor] = {}
        """Role → the one thread its engines run on, bar the cloud engines: cloud engine, by
        name → its own threads."""
        self._tts: dict[str, Engines[Tts]] = {}
        """Language → the engines that speak it."""
        self._defaults: dict[str, VoiceConfig] = {}
        """Language → its default Voice."""
        self._vad: Runner[Vad] | None = None
        self._turn: Runner[Turn] | None = None
        self._stt: dict[str, Engines[Stt]] = {}
        """Language → the engines that hear it."""
        self._runs: dict[tuple[str, str], Run] = {}
        """(device_id, capability) → the newest Run on the speaker, until it's over."""
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Make and warm up the engines, and check them against the config. Raises
        `ConfigError`."""
        self._config.check()
        try:
            await self._start_tts()
            await self._start_listening()
        except BaseException:
            self.close()
            raise

    async def _start_tts(self) -> None:
        self._tts = await self._load_languages(self._config.tts, Tts, "speak")
        for voice in self._config.voices:
            self._defaults.setdefault(voice.lang, voice)
            engines = self._tts[voice.lang]
            _check_voice(voice, engines.engine, voice.engine_voice)
            if engines.stand_in is not None:
                _check_voice(voice, engines.stand_in, voice.fallback_voice, "its fallback_voice")

    async def _start_listening(self) -> None:
        config = self._config
        if config.vad is not None:
            self._vad = _listening(await self._load(config.vad, Vad))
        if config.turn is not None:
            self._turn = _listening(await self._load(config.turn, Turn))
        self._stt = await self._load_languages(config.stt, Stt, "hear")
        for engines in self._stt.values():
            for runner in engines.runners:
                _listening(runner)

    async def _load_languages[E: Engine](
        self, languages: dict[str, EngineNames], role: type[E], verb: str
    ) -> dict[str, Engines[E]]:
        """Make the engines that serve `role` in each language, once each, and check that
        they `verb` it, and that a local engine stands in for each cloud engine."""
        loaded: dict[str, Runner[E]] = {}
        served: dict[str, Engines[E]] = {}
        for lang, names in languages.items():
            runners: list[Runner[E]] = []
            for name in engine_names(names):
                if name not in loaded:
                    loaded[name] = await self._load(name, role)
                runner = loaded[name]
                if (known := runner.engine.languages) and lang not in known:
                    raise ConfigError(
                        f"speech engine {name!r} doesn't {verb} {lang}, only {', '.join(known)}"
                    )
                runners.append(runner)
            engine, *stand_in = runners
            served[lang] = Engines(engine, _stand_in(engine, stand_in), self._timing)
        return served

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
        then_listen: ThenListen | None = None,
        stop: asyncio.Event | None = None,
    ) -> JsonObject:
        """Speak `text` on a speaker, after the `say`s queued on it, or cutting them off if
        `replace`; then, if the speaker drains after it, listen on the device's mic as
        `then_listen` says. Returns how it went once it has played. Setting `stop` cuts it,
        `stopped`. Raises `HubError`."""
        say = self._say(session, device, capability, Script.whole(text), voice, lang)
        say.then = self._then(session, device, then_listen)
        self._more(say)
        run = self._run(session, device, capability, replace)
        run.add(say)
        if stop is not None:
            stopping = asyncio.ensure_future(stop.wait())
            stopping.add_done_callback(lambda _: run.cut(say))
            say.done.add_done_callback(lambda _: stopping.cancel())
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
        then_listen: ThenListen | None = None,
    ) -> int:
        """Start a `say` whose text comes in with `say_append`, queued, and listening then,
        as `say` would be. Returns its handle. Raises `HubError`; once it's open, the error
        its speech failed to start with comes from `say_append` and `say_close`."""
        say = self._say(session, device, capability, Script(), voice, lang)
        say.then = self._then(session, device, then_listen)
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

    async def listen(
        self,
        session: Session,
        device: DeviceRecord,
        capability: Capability,
        *,
        lang: str | None,
        utterances: UtteranceOptIn,
        stop: asyncio.Event | None = None,
    ) -> tuple[JsonObject, int | None]:
        """Hear an Utterance on a mic. Returns the `utterance.end` params once it's delivered,
        and the handle its audio went to the agent on, if it did. Setting `stop` ends it
        `stopped`. Raises `HubError`."""
        listen = self._listen(
            self._ears(lang), session, device, capability, "listen", lang, utterances
        )
        await listen.start()
        if stop is not None:
            stopping = asyncio.ensure_future(stop.wait())
            stopping.add_done_callback(lambda _: listen.stop("stopped"))
            listen.done.add_done_callback(lambda _: stopping.cancel())
        return await asyncio.shield(listen.done), listen.handle

    def utterance_stop(self, session: Session, utterance_id: int) -> None:
        """End the Session's Utterance `utterance_id`, `stopped`, if it's under way."""
        for listen in list(session.listens):
            if listen.id == utterance_id:
                listen.stop("stopped")

    def talk(self, session: Session, device: DeviceRecord, mic: Capability) -> Listener:
        """Push-to-talk on `mic`, on a stream the Reflex opened: an Utterance for the Session,
        which the release ends. Returns what hears the stream. Raises `HubError`."""
        ears = self._ears(None) if self._stt else None
        listen = self._listen(ears, session, device, mic, "button", None, session.utterances)
        return listen.listener()

    async def session_ended(self, session: Session) -> None:
        """End the Session's listens, `disconnected`, and wait until they're delivered."""
        listens = list(session.listens)
        for listen in listens:
            listen.stop("disconnected")
        if listens:
            await asyncio.wait([listen.done for listen in listens])

    def _then(
        self, session: Session, device: DeviceRecord, then: ThenListen | None
    ) -> Callable[[], JsonObject] | None:
        """What a `say` with `then` does once the speaker drains after it: open the device's
        mic, for an Utterance or raw. Raises `HubError`."""
        if then is None:
            return None
        mic = next((c for c in device.manifest.capabilities if c.kind == "mic"), None)
        if mic is None:
            raise HubError("unsupported", f"{device.name} has no mic")
        if then.raw:

            def raw() -> JsonObject:
                self._spawn(self._raw_mic(session, device, mic, then.utterances.rate))
                return {}

            return raw
        ears = self._ears(then.lang)

        def open_mic() -> JsonObject:
            listen = self._listen(
                ears, session, device, mic, "then_listen", then.lang, then.utterances
            )
            self._spawn(listen.start_or_end())
            return {"utterance_id": listen.id}

        return open_mic

    def _listen(
        self,
        ears: Ears | None,
        session: Session,
        device: DeviceRecord,
        mic: Capability,
        trigger: Trigger,
        lang: str | None,
        utterances: UtteranceOptIn,
    ) -> Listen:
        return Listen(
            ears,
            self._control,
            self._timing,
            session,
            device,
            mic,
            trigger=trigger,
            lang=lang,
            utterances=utterances,
        )

    async def _raw_mic(
        self, session: Session, device: DeviceRecord, mic: Capability, rate: int
    ) -> None:
        try:
            await self._control.stream_start(session, device, mic, rate, None)
        except HubError as e:
            _log.info("%s/%s: then_listen's raw mic didn't start: %s", device.name, mic.id, e)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ears(self, lang: str | None) -> Ears:
        """What a listen in `lang`, if given, hears with. Raises `HubError`."""
        if not self._stt:
            raise HubError("unsupported", "the hub has no STT engine")
        if lang is None:
            stt = self._stt.get(self._config.lang, next(iter(self._stt.values())))
        elif (stt := self._stt.get(lang)) is None:
            raise HubError("unsupported", f"no STT engine hears {lang!r}")
        assert self._vad is not None  # the config checked
        return Ears(self._vad, self._turn, stt)

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
        return EngineVoice(self._tts[voice.lang], voice.engine_voice, voice.fallback_voice)

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
                run.followed()
                after = run
        run = self._runs[key] = Run(self._config, self._control, session, device, capability, after)
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

    async def _load[E: Engine](self, name: str, role: type[E]) -> Runner[E]:
        """Make engine `name` on `role`'s thread, or on threads of its own if it's a cloud
        engine, and warm it up there."""
        config = self._config.engines[name]
        module, _, qualname = config.cls.partition(":")
        try:
            cls = functools.reduce(getattr, qualname.split("."), importlib.import_module(module))
        except (ImportError, AttributeError) as e:
            raise ConfigError(f"speech engine {name!r}: can't import {config.cls}: {e}") from e
        if not isinstance(cls, type) or not issubclass(cls, role):
            raise ConfigError(f"speech engine {name!r}: {config.cls} isn't a {role.__name__}")
        key = name if cls.cloud else role
        executor = self._executors.get(key)
        if executor is None:
            executor = self._executors[key] = ThreadPoolExecutor(
                THREADS if cls.cloud else 1,
                thread_name_prefix=f"speech-{name if cls.cloud else role.__name__.lower()}",
            )
        loop = asyncio.get_running_loop()
        try:
            engine = await loop.run_in_executor(executor, functools.partial(cls, **config.params))
            await loop.run_in_executor(executor, engine.warm_up)
        except Exception as e:
            raise ConfigError(f"speech engine {name!r}: {e}") from e
        return Runner(name, engine, executor)


def _listening[E: Engine](runner: Runner[E]) -> Runner[E]:
    """An engine that takes the devices' audio as it is. Raises `ConfigError`."""
    if runner.engine.rate != FORMAT.rate:
        raise ConfigError(
            f"speech engine {runner.name!r} hears at {runner.engine.rate} Hz: the hub listens at "
            f"{FORMAT.rate} Hz only"
        )
    return runner


def _stand_in[E: Engine](engine: Runner[E], stand_in: list[Runner[E]]) -> Runner[E] | None:
    """The local engine that stands in for `engine`, if it's a cloud engine; None if it's a
    local one. Raises `ConfigError`."""
    if not engine.engine.cloud:
        if stand_in:
            raise ConfigError(
                f"speech engine {engine.name!r} isn't a cloud engine: it needs no stand-in"
            )
        return None
    if not stand_in:
        raise ConfigError(
            f"speech engine {engine.name!r} is a cloud engine: a local one must stand in for it"
        )
    [local] = stand_in
    if local.engine.cloud:
        raise ConfigError(f"speech engine {local.name!r} is a cloud engine: it can't stand in")
    return local


def _check_voice(voice: VoiceConfig, tts: Runner[Tts], name: str, key: str = "") -> None:
    """Raises `ConfigError` if `tts` has no voice `name` for `voice`, as its `key` says."""
    if tts.engine.voices is not None and name not in tts.engine.voices:
        raise ConfigError(
            f"Voice {voice.id!r}: speech engine {tts.name!r} has no voice {name!r}"
            + (f": set {key}" if key else "")
        )


def _retrieve(done: asyncio.Future[JsonObject]) -> None:
    """Mark the error a `say` failed with as seen."""
    if not done.cancelled():
        done.exception()
