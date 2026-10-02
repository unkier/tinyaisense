"""netem through the narrow-sudo wrapper, in its dry run, which prints what it would run: each
scenario shapes the traffic to and from the Echo's IP only, both ways, and the wrapper takes
nothing it hasn't checked."""

import os
import shlex
from pathlib import Path

import pytest
from tinyaisense_hil.netem import SCENARIOS, Netem, NetemError

WRAPPER = Path(__file__).parents[1] / "netem" / "tinyaisense-netem"
IFACE = next(name for name in sorted(os.listdir("/sys/class/net")) if name != "lo")
ECHO = "192.168.0.103"


def netem(ip: str = ECHO, iface: str = IFACE) -> Netem:
    return Netem((str(WRAPPER), "--dry-run"), iface, ip)


def commands(output: str) -> list[list[str]]:
    return [shlex.split(line) for line in output.splitlines()]


async def test_a_scenario_shapes_only_what_goes_to_and_comes_from_the_echo() -> None:
    ran = commands(await netem().apply("loss2"))

    filters = [command[command.index("match") :] for command in ran if "filter" in command]
    assert filters == [
        ["match", "ip", "dst", f"{ECHO}/32", "flowid", "1:4"],  # to the Echo: band 4, netem's
        ["match", "ip", "src", f"{ECHO}/32", "action", "mirred", "egress", "redirect", "dev",
         "tas-ifb"],  # from it: through the IFB, and its netem
    ]  # fmt: skip
    netems = [command[command.index("netem") + 1 :] for command in ran if "netem" in command]
    assert netems == [["loss", "random", "2%"]] * 2  # to the Echo, and from it


@pytest.mark.parametrize(
    ("scenario", "params"),
    [
        ("loss2", "loss random 2%"),
        ("loss5-bursts", "loss gemodel 1.32% 25% 100% 0%"),
        ("jitter30", "delay 15ms 15ms rate 1gbit"),
        ("blackout", "loss random 100%"),
    ],
)
async def test_each_scenario_has_its_netem(scenario: str, params: str) -> None:
    ran = await netem().apply(scenario)

    assert f"netem {params}\n" in ran


def test_every_scenario_is_known() -> None:
    assert SCENARIOS == ("loss2", "loss5-bursts", "jitter30", "blackout")


async def test_a_scenario_starts_from_a_clear_interface() -> None:
    applied = commands(await netem().apply("jitter30"))
    cleared = commands(await netem().clear())

    assert applied[: len(cleared)] == cleared
    assert [" ".join(command) for command in cleared] == [
        f"tc qdisc del dev {IFACE} root",
        f"tc qdisc del dev {IFACE} ingress",
        "ip link del tas-ifb",
    ]


@pytest.mark.parametrize(
    ("ip", "iface", "scenario"),
    [
        ("192.168.0.300", IFACE, "loss2"),
        ("192.168.0.03", IFACE, "loss2"),
        ("192.168.0.3; reboot", IFACE, "loss2"),
        ("0/0", IFACE, "loss2"),
        (ECHO, "lo", "loss2"),
        (ECHO, "no-such-if0", "loss2"),
        (ECHO, "../../etc", "loss2"),
        (ECHO, IFACE, "delay 1s"),
    ],
)
async def test_the_wrapper_refuses_what_it_cant_check(ip: str, iface: str, scenario: str) -> None:
    with pytest.raises(NetemError):
        await netem(ip, iface).apply(scenario)


async def test_a_missing_wrapper_says_how_to_install_it() -> None:
    with pytest.raises(NetemError, match=r"install\.sh"):
        await Netem(("/nonexistent/tinyaisense-netem",), IFACE, ECHO).apply("loss2")
