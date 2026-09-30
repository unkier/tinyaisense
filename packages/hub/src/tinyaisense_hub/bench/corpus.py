"""The speech bench's corpus: human speech, to time the STT on, and sentences, to time the TTS
on. The bench hears and speaks them again to load the hub while the button is pressed.

It's 30 utterances from LibriSpeech's test-clean set (English, CC BY 4.0) and 30 from
Common Voice 17.0's Russian test set (CC0), taken from a mirror, since Mozilla took Common
Voice off Hugging Face. They were picked at random, seeded, one per speaker, among those with
2 to 7 s of speech, and the Common Voice ones among those with 2 up-votes and no down-votes.
Each is pinned by the sha256 of its file in its archive, and by where its speech starts and
ends, which Silero VAD v6 heard at 16 kHz.
"""

# The transcripts and sentences are in Russian too, and some are too long for a line.
# ruff: noqa: E501, RUF001

import hashlib
import os
import tarfile
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
import soundfile
import soxr

from ..config import data_dir
from ..streams import FORMAT

PRE_ROLL_MS = 300
"""How much of a clip's audio before its speech is heard, at most."""


class BenchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Archive:
    name: str
    """Its dir within the corpus dir."""
    url: str


@dataclass(frozen=True, slots=True)
class Clip:
    archive: Archive
    member: str
    """Its path within its archive."""
    sha256: str
    lang: str
    text: str
    """What's said, as the corpus transcribes it."""
    speech: tuple[int, int]
    """Where its speech starts and ends, in ms."""

    @property
    def name(self) -> str:
        """Its file's name, within its archive's dir."""
        return PurePosixPath(self.member).name


LIBRISPEECH = Archive(
    "librispeech-test-clean", "https://www.openslr.org/resources/12/test-clean.tar.gz"
)
COMMON_VOICE_RU = Archive(
    "common-voice-17.0-ru-test",
    "https://huggingface.co/datasets/fsicoli/common_voice_17_0/resolve/"
    "8262c16bf297c87a9cd88c51997c4758ed7a8ba2/audio/ru/test/ru_test_0.tar",
)


def _en(utterance: str, sha256: str, text: str, speech: tuple[int, int]) -> Clip:
    """LibriSpeech's `speaker/chapter/utterance`."""
    member = f"LibriSpeech/test-clean/{utterance}.flac"
    return Clip(LIBRISPEECH, member, sha256, "en", text, speech)


def _ru(clip: str, sha256: str, text: str, speech: tuple[int, int]) -> Clip:
    """Common Voice's clip number `clip`."""
    member = f"ru_test_0/common_voice_ru_{clip}.mp3"
    return Clip(COMMON_VOICE_RU, member, sha256, "ru", text, speech)


CORPUS = (
    _en(
        "2830/3980/2830-3980-0010",
        "91e4b64a72fc87c71a7ae512fbeaf1c835727910c393a681a254a38dcbc55989",
        "EITHER HE CALLS MINISTERS THROUGH THE AGENCY OF MEN OR HE CALLS THEM DIRECTLY AS HE CALLED THE PROPHETS AND APOSTLES",
        (410, 6240),
    ),
    _en(
        "672/122797/672-122797-0030",
        "a0abef00544a7f476df3df7134de44602cb9496845125eea4a22f872d754b6fd",
        "PERHAPS THE OTHER TREES FROM THE FOREST WILL COME TO LOOK AT ME",
        (600, 4320),
    ),
    _en(
        "7021/79740/7021-79740-0003",
        "758a320b5aa421050fb27349b02c74093e8a70e475ef5905016936f0f377cc90",
        "TO GIVE AN IDEA OF THESE CONVERSATIONS I WILL REPORT ONE OF THEM IN FULL",
        (440, 4680),
    ),
    _en(
        "2961/961/2961-961-0006",
        "4dc86bba4f74f8562153f964a721e827463c76850bb6eee23d7c8aab3150717c",
        "AND WHAT WAS THE SUBJECT OF THE POEM SAID THE PERSON WHO MADE THE REMARK",
        (540, 4260),
    ),
    _en(
        "8463/294828/8463-294828-0014",
        "b61f11aa0c2b3d1adca49e6199328637c6ecc84c003dab03fc8c97b95cab4c63",
        "THERE WAS GOOD REASON TO STOP AND THINK EVEN FOR THE WORLD'S MOST EMOTIONLESS MAN",
        (410, 5540),
    ),
    _en(
        "7176/92135/7176-92135-0013",
        "edd1c606017f0d1a66a2f23a28c4a8794a7e0edcca42e215b875d227f784e3d7",
        "WE MODERNS HOWEVER SEE THE ABSURDITY OF IT",
        (190, 3140),
    ),
    _en(
        "4077/13751/4077-13751-0019",
        "525405db54340f7feabcd09898c2f259b22ce58931c5dc488706269648683e1a",
        "WHO BEGAN THE QUARREL WAS IT THE MORMONS",
        (440, 2690),
    ),
    _en(
        "6829/68769/6829-68769-0007",
        "e765c65528b7a876dc8271959b20905accaf0713b75fff9a261cca1fcc93a5f3",
        "BUT UNDER THE CIRCUMSTANCES I DOUBT IF SUCH AN ARRANGEMENT COULD BE MADE",
        (220, 3590),
    ),
    _en(
        "4507/16021/4507-16021-0003",
        "090444ed0871bdaaa1d25065939bbd756ef74ec3398622591d1073b90c8026ea",
        "SHE HAS A SON THEFT AND A DAUGHTER HUNGER",
        (640, 4640),
    ),
    _en(
        "3575/170457/3575-170457-0027",
        "3ed3fecfe9704df156f249291366e1873a25427efe131304be9e147dce150eb7",
        "I CANNOT DENY MYSELF THE GRATIFICATION OF INSERTING SOUTHEY'S REPLY",
        (570, 4200),
    ),
    _en(
        "8455/210777/8455-210777-0020",
        "02236ecd1be6de19202a31bf6c02c554ab97ed593d60aa62ed060eeebaa544de",
        "OH YES SAID JACK AND I'M NOWHERE",
        (570, 2880),
    ),
    _en(
        "1580/141083/1580-141083-0041",
        "519b0f10c4b4c9375239d9a198db84ab45d27e3d0906a81af68f07dfecc8c967",
        "LET US HEAR THE SUSPICIONS I WILL LOOK AFTER THE PROOFS",
        (410, 3110),
    ),
    _en(
        "6930/81414/6930-81414-0021",
        "aa6ca39905cd3a4b998ee5801778878d1bf50434246d01229950783e6c84960c",
        "A TERRIBLE THOUGHT FLASHED INTO MY MIND",
        (540, 3140),
    ),
    _en(
        "1995/1826/1995-1826-0012",
        "56d936811e760549d7f7f5d63ae68e916cd0b82859a1ea10a5ee7471558f83c8",
        "SHE WANTED A GLANCE OF THE NEW BOOKS AND PERIODICALS AND TALK OF GREAT PHILANTHROPIES AND REFORMS",
        (320, 6080),
    ),
    _en(
        "7127/75946/7127-75946-0025",
        "fcb6fb18676bed01bec7958fd678bb315531581168b6e48379b0442a88246ac8",
        "THE BALLET BEGAN THE EFFECT WAS MORE THAN BEAUTIFUL",
        (570, 3720),
    ),
    _en(
        "3570/5695/3570-5695-0013",
        "059ca76eeb894ed8e3a9ed001ab5a75166caa02feb90e596a28471a8ae509b5a",
        "CONSUMPTION BECOMES A LARGER ELEMENT IN THE STANDARD OF LIVING IN THE CITY THAN IN THE COUNTRY",
        (410, 4580),
    ),
    _en(
        "4992/23283/4992-23283-0003",
        "c047e460bc13041cbae6f71ccc8c6dc3839dded56059d3af68716bd26d5d4227",
        "SO THERE IS TO ME ADDED SANDFORD WITH A SARCASTIC SNEER",
        (800, 4390),
    ),
    _en(
        "7729/102255/7729-102255-0029",
        "c2de8ed696b84e941bd0c95176c11cde1b5ed87a606288775215868c1250141e",
        "TEN DAYS WERE CONSUMED IN THESE NEGOTIATIONS BUT THE SPIRIT OF VENGEANCE REFUSED TO YIELD",
        (600, 6920),
    ),
    _en(
        "1089/134691/1089-134691-0001",
        "22e7e8b3d30d19b754965bafe3ffe486a5a05afaf4ef6f07540ac2ef8c8a9656",
        "FOR A FULL HOUR HE HAD PACED UP AND DOWN WAITING BUT HE COULD WAIT NO LONGER",
        (410, 4930),
    ),
    _en(
        "61/70968/61-70968-0012",
        "980dd86b87c173dc5c0e5395de5619c95523fb59e8dfbece50108afb4ce2277f",
        "CRIES OF A NOTTINGHAM A NOTTINGHAM",
        (410, 2600),
    ),
    _en(
        "1320/122612/1320-122612-0004",
        "8d9a1195950140b1799204ed5d21577708d2398a0e7b3b5936dace5b03c66139",
        "DISTRUSTING HIS OWN JUDGMENT HIS APPEALS TO THE OPINION OF CHINGACHGOOK WERE FREQUENT AND EARNEST",
        (350, 5990),
    ),
    _en(
        "2300/131720/2300-131720-0038",
        "c5906494d7d45442ea8d1c8fd544097d57df7bf77b8698b474b867f711677772",
        "HE FELT HE WAS UP AGAINST IT AND THAT PERHAPS ANOTHER KIND OF A JOB WOULD SUIT HIM BETTER",
        (320, 5160),
    ),
    _en(
        "5683/32866/5683-32866-0020",
        "3593b0afba4d87450f3b5a8fda5c8a5647f769b3a89b24f5fa4d8c437f1f3ebe",
        "THE FLOOR MORE THAN ANYTHING ELSE SHOWED THE GREAT AGE OF THE ROOM",
        (570, 3880),
    ),
    _en(
        "4446/2275/4446-2275-0016",
        "2e5bfa0d6b101ca5337b2b3fb1602728cbed79fe861d4373b1abc23bc4d31a7e",
        "HILDA WATCHED HIM FROM HER CORNER TREMBLING AND SCARCELY BREATHING DARK SHADOWS GROWING ABOUT HER EYES IT",
        (540, 7300),
    ),
    _en(
        "5639/40744/5639-40744-0007",
        "f0f12cbe75ff1e245af978031a38cf5b34bc408b18cb5617019d425c451cc7e2",
        "MEANWHILE RODOLFO HAD LEOCADIA SAFE IN HIS CUSTODY AND IN HIS OWN APARTMENT",
        (540, 5800),
    ),
    _en(
        "237/126133/237-126133-0019",
        "f142c5d32add1197ca2690249b29ff7913a2c55225f78c30ba30d343c99b049d",
        "DEAR ME EJACULATED THE OLD GENTLEMAN IN THE UTMOST AMAZEMENT AND SUCH A TIME AS I'VE HAD TO GET HER HERE TOO",
        (540, 7040),
    ),
    _en(
        "2094/142345/2094-142345-0039",
        "21d7d00350a4ca2b0109da67cfad724f36979e7c1b2e098f9327bc8c4f433435",
        "I'VE STRONG ASSURANCE THAT NO EVIL WILL HAPPEN TO YOU AND MY UNCLE AND THE CHILDREN FROM ANYTHING I'VE DONE",
        (320, 6080),
    ),
    _en(
        "1188/133604/1188-133604-0043",
        "9c55c330517fcd785e205a3a4e9b4612355b703201a002937b43795555ba92d2",
        "SEE THAT YOUR LIVES BE IN NOTHING WORSE THAN A BOY'S CLIMBING FOR HIS ENTANGLED KITE",
        (410, 4390),
    ),
    _en(
        "121/121726/121-121726-0012",
        "6d1fab44fd2e6930a69e1f6f7e52a141ed5290b9a45f56df5e0c3177fa05c6ba",
        "HUSSY WOMAN AND BOND TIE",
        (190, 3750),
    ),
    _en(
        "260/123286/260-123286-0024",
        "756bb76a0e8cf38cb43f9e92724c8a3f56af63c0676622e5b8e03367b49edd27",
        "THERE'S A WHALE A WHALE CRIED THE PROFESSOR",
        (570, 2920),
    ),
    _ru(
        "23436347",
        "a4f991e7b946b37994ef29dfb0482ea6ae71baa8a6d0b807f506e28e5c212d5f",
        "Враждебные чувства, имеющие несколько более сложную структуру, не являются исключением из этого.",
        (510, 6400),
    ),
    _ru(
        "37140165",
        "6f247a783dd015f18c51d9f92c97cfbd5e2b6253877a7c8253267d10dcf65629",
        "Я слышал, что Либих сделал удивительные открытия насчет удобрения полей.",
        (700, 4420),
    ),
    _ru(
        "26737974",
        "f437c8e6cba305825d01329a5168570b62e2785e59184646fa3a031bea448281",
        "Китай, со своей стороны, поддерживает такую деятельность.",
        (920, 3650),
    ),
    _ru(
        "37564164",
        "c32301b06110d3224f722d193bcf0535b3976f8f097e4b15821496bfc21fe6ce",
        "У меня руки опустились уже на второй месяц работы.",
        (1150, 3620),
    ),
    _ru(
        "28893071",
        "0b11acd82e59828456f3fa310fc197703a2a498f92928a9dca9596cdf481063b",
        "Нам было очень весело, мы поплыли по течению и встретили рака.",
        (410, 4320),
    ),
    _ru(
        "20506378",
        "3403e3ee6b6c4c98ba3c881916d8cdca0446d75b9dcf3f7f393b22cac366c986",
        "Он как-то тихо угас, точно догоревшая свеча.",
        (600, 4360),
    ),
    _ru(
        "39569056",
        "1b4902db12f42c06cd5e7ea4ad0915119318da2c1e6c8e998bf8b747a0bb3bcb",
        "Революция ассоциируется с потрясениями.",
        (480, 2790),
    ),
    _ru(
        "21638094",
        "6ca31f51c0ba6c13bb7f8c98f4184ce0258f886b223646a7dcea9806b143d20f",
        "Я не говорю о непостоянном членстве.",
        (1180, 4230),
    ),
    _ru(
        "35306760",
        "4f3d0079cba3440c817c880f328321705d9bfb80945d8f0db29d6674ba417052",
        "Но пледа в посылке не оказалось.",
        (990, 3330),
    ),
    _ru(
        "23009934",
        "b9bc8b5fc5dbf9dd34e31a0a267c326c7eaadabb3abb241639e64e2bfa5d0835",
        "Мы оставим превосходную гражданскую службу.",
        (670, 3520),
    ),
    _ru(
        "27309039",
        "8879c71e3ec7dd5e5d027d52b1314b0f7275b740c40e86b19e3b221242450b03",
        "Его избрание свидетельствует об уважении к братскому Государству Катар и арабскому миру в целом.",
        (640, 6560),
    ),
    _ru(
        "26259348",
        "5f17ef39fafaf86ea141611ca43abefbf6c992f21bb469c06d5ea37ff22b1e41",
        "Позиция Китая носит четкий характер.",
        (860, 3270),
    ),
    _ru(
        "19202507",
        "0ae979282689ff1936c8cf25d51419d075101f106299211d10f1e50672594193",
        "Я предоставляю слово представителю Фиджи.",
        (320, 2530),
    ),
    _ru(
        "27727109",
        "fbac9f7e279a083ba0d75a75b18ff6b51df43395fa8fcfbdde3c623d0c961fb3",
        "Марья Ивановна от меня не отходила.",
        (1020, 3080),
    ),
    _ru(
        "20423236",
        "621417f86bc8450d4361a144d2c97ff2158736587a2105f19456401c81577911",
        "Необходимо также в срочном порядке продолжить работу по проведению внутренней реформы Организации.",
        (410, 5990),
    ),
    _ru(
        "19504491",
        "0e476650a0dfb7058a612325fd33e0b06ba50bb9ac4adfa26cf5ee697203ed5c",
        "В отношении данного проекта резолюции все еще существуют серьезные проблемы.",
        (1020, 6150),
    ),
    _ru(
        "35052123",
        "df9cf1cd59f1af31bdf5a7bd65ab6f80fada735defca1806e21625871ef29a0d",
        "За этот сезон нам показали всего два крупных тайтла, один из которых релизнут послезавтра.",
        (1050, 8000),
    ),
    _ru(
        "19567117",
        "a31ad5712d2d0c4f1bf3b939be0de91de7fe4b3a17eda2173fc5011ca2c2a506",
        "Мы беспомощно наблюдаем за тем, как эта глобальная трагедия разворачивается на наших глазах.",
        (600, 6120),
    ),
    _ru(
        "23551474",
        "d84457f7d0669ca1f38291de0be75d2232511121a694e7f14bb0d457de170363",
        "Однако они знают, что всякое новое дело сомнительно.",
        (670, 3460),
    ),
    _ru(
        "22711211",
        "067d5c02831329b78bdf19c659f1438abc99bce66b46d09f4bdec32f652d6e13",
        "Жизнь невозможна и немыслима без доступа к воде.",
        (670, 3400),
    ),
    _ru(
        "36894968",
        "186bb93c53369b29184aef861e7792a207defc73125e4f9c38f88ecacf1ddb32",
        "Архимед в бане, полоскаясь, наведен был на открытие своего гидростатического закона.",
        (830, 5830),
    ),
    _ru(
        "20177094",
        "a3738781f7bb26e58d54730c033877ed8b3fd534be3a65b67eeefe59016eb4ca",
        "Успешное развитие напрямую связано с повышением уровня развития наших финансовых и торговых партнеров.",
        (1080, 6150),
    ),
    _ru(
        "25769202",
        "5e1d7fd722b85761efde6a021632d3949939860ec8a68a761d8bbb2f29ad3e47",
        "Доклады Шестого комитета.",
        (860, 3170),
    ),
    _ru(
        "32850789",
        "f964af7c793ddcf41f436a07916299d906119b48cec0ef242a3c7f54290dd517",
        "Он поцеловал ее руку и подсел к ней.",
        (960, 3270),
    ),
    _ru(
        "26817662",
        "9bbcf184729c6f8ec6afd3617fbdb3722c9d8f62a9a7e5896cc0ddb206b7474c",
        "Убежден, что Вы будете успешно руководить работой Комитета.",
        (1210, 4840),
    ),
    _ru(
        "37789034",
        "adea785fdd065b3170eca041c6d78689bbcf32b50c888ce0506c64f20ee353fa",
        "Канавку она перелетела, как бы не замечая.",
        (920, 5160),
    ),
    _ru(
        "26889403",
        "3ee2a60a1dc43149770a0c6cdf507ca73ca0003e969a13e2ffd66b199acad5e7",
        "Я полагаю, что мы сделали шаги в правильном направлении.",
        (890, 3940),
    ),
    _ru(
        "27048546",
        "d8abb11e7bacba5b389dad5b37aa07131bb627f9da0b47aa9f4fd0e1411be5ab",
        "Необходимо организовать работу пользовательских приложений в новой кластерной среде",
        (730, 6690),
    ),
    _ru(
        "21698495",
        "385d3d68bf873a5135ea403476658fbc16ea355eb8db3cf9f8f07a684f84126f",
        "Наиболее серьезной угрозой является, вероятно, ядерный терроризм.",
        (730, 5570),
    ),
    _ru(
        "21299577",
        "5255dd6f17a6f1aaf68d6eae769a344e0708bca823bf0fc5b01ad20471386270",
        "Проект резолюции озаглавлен «Контроль над обычными вооружениями на региональном и субрегиональном уровнях».",
        (1120, 7360),
    ),
)


@dataclass(frozen=True, slots=True)
class Sentence:
    lang: str
    text: str


SENTENCES = (
    Sentence("en", "Sure, I've set a timer for ten minutes."),
    Sentence("en", "The weather today is sunny, with a high of eighteen degrees."),
    Sentence("en", "I couldn't find that song, but here's something similar."),
    Sentence("en", "Your next meeting starts in fifteen minutes."),
    Sentence("en", "Okay, turning off the kitchen lights."),
    Sentence("en", "It's a quarter past seven in the evening."),
    Sentence("en", "I've added milk and eggs to your shopping list."),
    Sentence("en", "Good morning! You have three new messages."),
    Sentence("en", "That depends on how long you want to cook it."),
    Sentence("en", "Here's what I found about the history of the Eiffel Tower."),
    Sentence("ru", "Хорошо, я поставила таймер на десять минут."),
    Sentence("ru", "Сегодня солнечно, днём до восемнадцати градусов."),
    Sentence("ru", "Я не нашла эту песню, но вот похожая."),
    Sentence("ru", "Ваша следующая встреча начнётся через пятнадцать минут."),
    Sentence("ru", "Хорошо, выключаю свет на кухне."),
    Sentence("ru", "Сейчас четверть восьмого вечера."),
    Sentence("ru", "Я добавила молоко и яйца в список покупок."),
    Sentence("ru", "Доброе утро! У вас три новых сообщения."),
    Sentence("ru", "Это зависит от того, сколько времени вы хотите его готовить."),
    Sentence("ru", "Вот что я нашла об истории Эйфелевой башни."),
)
"""What an agent might say: the TTS gate times each, from the request to its first frame."""


def corpus_dir() -> Path:
    return data_dir() / "bench"


def fetch(
    clips: Iterable[Clip], into: Path, report: Callable[[str], None] = lambda _: None
) -> None:
    """Take each clip out of its archive into a dir of its own in `into`, unless it's there
    already, checking it against its sha256. An archive is read only as far as its last
    clip still missing. `report` hears how it goes. Raises `BenchError` or `OSError`."""
    missing: dict[Archive, dict[str, Clip]] = {}
    for clip in clips:
        path = into / clip.archive.name / clip.name
        if not (path.exists() and _sha256(path.read_bytes()) == clip.sha256):
            missing.setdefault(clip.archive, {})[clip.member] = clip
    for archive, wanted in missing.items():
        report(f"{archive.name}: fetching {len(wanted)} clips from {archive.url}")
        _extract(archive, wanted, into / archive.name)


def _extract(archive: Archive, wanted: dict[str, Clip], into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    with (
        urllib.request.urlopen(archive.url, timeout=30) as response,
        tarfile.open(fileobj=response, mode="r|*") as tar,
    ):
        for member in tar:
            clip = wanted.pop(member.name, None)
            if clip is None:
                continue
            f = tar.extractfile(member)
            assert f is not None, f"{member.name} isn't a file"
            data = f.read()
            if (sha256 := _sha256(data)) != clip.sha256:
                raise BenchError(
                    f"{archive.name}/{clip.name} from {archive.url} has sha256 {sha256}, not "
                    f"the pinned {clip.sha256}"
                )
            part = into / (clip.name + ".part")
            part.write_bytes(data)
            os.replace(part, into / clip.name)
            if not wanted:
                return
    raise BenchError(f"{archive.url} has no {', '.join(wanted)}")


def speech(clip: Clip, into: Path) -> bytes:
    """The clip, as the device's mic would hear it, from `PRE_ROLL_MS` before its speech to its
    end: 16 kHz mono s16le. It must have been fetched into `into`."""
    samples, rate = soundfile.read(into / clip.archive.name / clip.name, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1, dtype=np.float32)
    if rate != FORMAT.rate:
        samples = soxr.resample(samples, rate, FORMAT.rate, quality="VHQ")
    start, end = (ms * FORMAT.rate // 1000 for ms in clip.speech)
    samples = samples[max(0, start - PRE_ROLL_MS * FORMAT.rate // 1000) : end]
    return (np.clip(samples, -1, 1) * 32767).round().astype("<i2").tobytes()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
