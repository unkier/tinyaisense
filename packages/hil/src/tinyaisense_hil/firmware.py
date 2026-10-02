"""The Echo's debug build (CONFIG_TAS_TEST_HOOKS), built with PlatformIO and flashed only when
the Echo runs another build: the firmware hash is the app's ELF sha256, which esptool writes
into the image, and which the console's `status` shows."""

import subprocess
from pathlib import Path

from .console import EchoStatus

FIRMWARE = Path(__file__).resolve().parents[4] / "firmware"
DEBUG_ENV = "m5stack-atom-echo-debug"
_DESC_AT = 0x20
"""The app description, `esp_app_desc_t`: after the 24-byte image header and the 8-byte
header of the first segment."""
_DESC_MAGIC = 0xABCD5432
_ELF_SHA_AT = _DESC_AT + 0x90
"""Its `app_elf_sha256`."""


class FirmwareError(Exception):
    pass


def elf_sha256(image: Path) -> str:
    """The ELF sha256 in the app image at `image`, in hex. Raises `FirmwareError`."""
    data = image.read_bytes()
    if int.from_bytes(data[_DESC_AT : _DESC_AT + 4], "little") != _DESC_MAGIC:
        raise FirmwareError(f"{image} has no app description: not an ESP-IDF app image")
    return data[_ELF_SHA_AT : _ELF_SHA_AT + 32].hex()


def needs_flash(echo: EchoStatus, built: str) -> bool:
    """Whether the Echo runs a build other than `built`, the ELF sha256 of a build."""
    return echo.elf is None or not built.startswith(echo.elf)


def build(pio: str) -> str:
    """Build the debug build, as far as it changed, and return its ELF sha256. Raises
    `FirmwareError`."""
    _pio(pio, "run", "-e", DEBUG_ENV)
    return elf_sha256(FIRMWARE / ".pio" / "build" / DEBUG_ENV / "firmware.bin")


def flash(pio: str, port: str) -> None:
    """Flash the debug build onto the device on serial `port`, which must be free. esptool
    resets it after, through its EN pin. Raises `FirmwareError`."""
    _pio(pio, "run", "-e", DEBUG_ENV, "-t", "upload", "--upload-port", port)


def _pio(pio: str, *args: str) -> None:
    try:
        done = subprocess.run(
            [pio, *args], cwd=FIRMWARE, capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise FirmwareError(f"can't run PlatformIO, {pio!r}: {e}") from None
    if done.returncode:
        tail = "\n".join((done.stdout + done.stderr).splitlines()[-30:])
        raise FirmwareError(f"pio {' '.join(args)} failed:\n{tail}")
