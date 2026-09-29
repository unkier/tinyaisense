"""The speaker Kind on a simulated device: docs/protocol/kinds/speaker.md."""


def volume_gain(level: int) -> float:
    """The software gain of a `set_volume` level: 0 is mute, 1 is -40 dB and 100 is 0 dB,
    evenly spaced in dB between."""
    if level == 0:
        return 0.0
    return 10 ** (-40 * (100 - level) / 99 / 20)


class SimSpeaker:
    """One speaker capability. Its volume lasts until the device powers off."""

    def __init__(self, level: int) -> None:
        self.level = level
        """The volume, 0 to 100."""

    @property
    def gain(self) -> float:
        """What the speaker scales audio by, at its volume."""
        return volume_gain(self.level)
