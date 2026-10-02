"""The flash decision: the debug build is flashed only when the ELF sha256 the Echo runs differs
from the one just built."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from tinyaisense_hil.console import EchoStatus
from tinyaisense_hil.firmware import FirmwareError, elf_sha256, needs_flash

SHA = hashlib.sha256(b"firmware.elf").hexdigest()
ECHO = EchoStatus(
    device_id="f4650b08536c",
    board="m5stack-atom-echo",
    firmware="83277b9",
    elf=SHA[:16],
    ip="192.168.0.103",
    hub="auto",
    name="echo-536c",
    uptime_s=10,
)


def image(sha: str, magic: bytes = bytes.fromhex("3254cdab")) -> bytes:
    """An app image as esptool lays it out: a 24-byte image header, an 8-byte segment
    header, then the app description, whose ELF sha256 is 0x90 into it."""
    desc = magic + bytes(0x8C) + bytes.fromhex(sha) + bytes(64)
    return bytes([0xE9]) + bytes(31) + desc


def test_the_built_images_elf_sha256_is_read_from_its_app_description(tmp_path: Path) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(image(SHA))

    assert elf_sha256(path) == SHA


def test_an_image_with_no_app_description_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(image(SHA, magic=bytes(4)))

    with pytest.raises(FirmwareError, match="app description"):
        elf_sha256(path)


def test_the_echo_is_flashed_only_when_it_runs_another_build() -> None:
    assert not needs_flash(ECHO, SHA)
    assert needs_flash(ECHO, hashlib.sha256(b"another").hexdigest())
    assert needs_flash(replace(ECHO, elf=None), SHA)  # firmware that doesn't say
