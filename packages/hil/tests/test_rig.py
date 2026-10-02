"""The rig config, `hil.toml`: the Echo's serial port, and what to reach it by."""

from pathlib import Path

import pytest
from tinyaisense_hil.rig import NETEM, Rig, RigError, find_rig, load_rig


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_a_rig_needs_only_the_echos_serial_port(tmp_path: Path) -> None:
    rig = load_rig(write(tmp_path / "hil.toml", 'port = "/dev/ttyUSB0"\n'))

    assert rig == Rig(port="/dev/ttyUSB0")
    assert rig.echo_ip is None
    assert rig.iface is None
    assert rig.hub_ip is None
    assert rig.state is None
    assert rig.netem == NETEM == ("sudo", "-n", "/usr/local/sbin/tinyaisense-netem")
    assert rig.pio == "pio"
    assert rig.soak_minutes == 60


def test_a_rig_takes_every_setting(tmp_path: Path) -> None:
    rig = load_rig(
        write(
            tmp_path / "hil.toml",
            """
            port = "/dev/ttyUSB1"
            echo_ip = "192.168.0.103"
            iface = "enp5s0"
            hub_ip = "192.168.0.106"
            state = "~/hub/state.json"
            netem = ["sudo", "-n", "/opt/netem"]
            pio = "~/.platformio/penv/bin/pio"
            soak_minutes = 5
            """,
        )
    )

    assert rig == Rig(
        port="/dev/ttyUSB1",
        echo_ip="192.168.0.103",
        iface="enp5s0",
        hub_ip="192.168.0.106",
        state=str(Path.home() / "hub/state.json"),
        netem=("sudo", "-n", "/opt/netem"),
        pio=str(Path.home() / ".platformio/penv/bin/pio"),
        soak_minutes=5,
    )


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "port"),
        ("port = 1", "port"),
        ('port = "/dev/ttyUSB0"\nbaud = 9600', "baud"),
        ('port = "/dev/ttyUSB0"\necho_ip = "192.168.0.300"', "echo_ip"),
        ('port = "/dev/ttyUSB0"\nhub_ip = "hub.local"', "hub_ip"),
        ('port = "/dev/ttyUSB0"\nstate = ""', "state"),
        ('port = "/dev/ttyUSB0"\nnetem = []', "netem"),
        ('port = "/dev/ttyUSB0"\nnetem = "sudo netem"', "netem"),
        ('port = "/dev/ttyUSB0"\nsoak_minutes = 0', "soak_minutes"),
        ('port = "/dev/ttyUSB0"\nsoak_minutes = true', "soak_minutes"),
        ("port = ", "hil.toml"),
    ],
)
def test_a_rig_refuses_what_it_cant_use(tmp_path: Path, text: str, why: str) -> None:
    with pytest.raises(RigError, match=why):
        load_rig(write(tmp_path / "hil.toml", text))


def test_a_missing_rig_says_where_it_was_looked_for(tmp_path: Path) -> None:
    with pytest.raises(RigError, match=r"hil\.toml"):
        load_rig(tmp_path / "hil.toml")


def test_the_rig_is_named_by_env_then_found_here_then_in_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here, config = tmp_path / "here", tmp_path / "config"
    (config / "tinyaisense").mkdir(parents=True)
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("TINYAISENSE_HIL_RIG", raising=False)

    assert find_rig() == here / "hil.toml"  # none anywhere: where to put one

    write(config / "tinyaisense" / "hil.toml", "")
    assert find_rig() == config / "tinyaisense" / "hil.toml"

    write(here / "hil.toml", "")
    assert find_rig() == here / "hil.toml"

    monkeypatch.setenv("TINYAISENSE_HIL_RIG", str(tmp_path / "named.toml"))
    assert find_rig() == tmp_path / "named.toml"
