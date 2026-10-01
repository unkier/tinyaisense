"""The device clock: docs/protocol/README.md. One crystal drives it and the audio, so clock
drift skews every `ts` and the mic and speaker sample rates alike."""

import time

SLACK_US = 1000
"""How early a loop timer may fire: uvloop's run on a millisecond clock. Audio clocked off the
device clock counts a period as done this close to its end."""


class DeviceClock:
    """µs since boot, plus the offset it booted with, running `ppm` fast against true time."""

    def __init__(self) -> None:
        self._ppm = 0.0
        self._true = time.monotonic_ns()
        """True time at the last rebase, in ns."""
        self._base = 0
        """The device clock at the last rebase, in µs."""
        self._booted = 0
        """The device clock at boot, in µs."""

    def boot(self, offset_us: int = 0) -> None:
        """Restart the clock at `offset_us`."""
        self._true = time.monotonic_ns()
        self._base = self._booted = offset_us

    def now(self) -> int:
        return self._base + round((time.monotonic_ns() - self._true) * self.rate / 1000)

    @property
    def uptime_us(self) -> int:
        return self.now() - self._booted

    @property
    def ppm(self) -> float:
        return self._ppm

    @ppm.setter
    def ppm(self, ppm: float) -> None:
        """From now on: the clock keeps its reading and changes its rate."""
        self._base, self._true = self.now(), time.monotonic_ns()
        self._ppm = ppm

    @property
    def rate(self) -> float:
        """Device µs per true µs."""
        return 1 + self._ppm / 1e6

    def delay(self, at: int) -> float:
        """True seconds until the clock reads `at` µs: 0 if it has."""
        return max(0.0, (at - self.now()) / self.rate / 1e6)
