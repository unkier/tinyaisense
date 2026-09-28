"""The Status a simulated device shows, from its layers: docs/protocol/kinds/device.md#status."""

from tinyaisense_sim.status import StatusLayers

PRIORITY = [
    "identify",
    "listening",
    "claim",
    "offline",
    "connecting",
    "pending",
    "no_config",
    "speaking",
    "thinking",
]
"""The Status table, highest first, down to the hub layer."""


def test_the_device_shows_the_highest_priority_state_that_applies() -> None:
    layers = StatusLayers(
        identify=True,
        listening=True,
        claimed=True,
        offline=True,
        connecting=True,
        pending=True,
        no_config=True,
        speaking=True,
        hub="thinking",
    )
    shown: list[str] = []

    for layer in PRIORITY[:-1]:
        shown.append(layers.shown)
        setattr(layers, "claimed" if layer == "claim" else layer, False)
    shown.append(layers.shown)

    assert shown == PRIORITY


def test_the_hub_layer_shows_when_nothing_else_applies() -> None:
    assert StatusLayers().shown == "no_agent"
    assert StatusLayers(hub="agent").shown == "agent"


def test_listening_shows_through_an_agent_claim() -> None:
    assert StatusLayers(claimed=True, listening=True).shown == "listening"


def test_an_agent_claim_suspends_every_state_below_it() -> None:
    layers = StatusLayers(claimed=True, offline=True, pending=True, speaking=True, hub="thinking")

    assert layers.shown == "claim"
