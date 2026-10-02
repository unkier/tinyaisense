"""netem between the hub PC and the Echo, through the narrow-sudo wrapper in `netem/`: each
scenario shapes the traffic to and from the Echo's IP, both ways, and nothing else."""

import asyncio
from collections.abc import Sequence

SCENARIOS = ("loss2", "loss5-bursts", "jitter30", "blackout")
"""What the wrapper knows: 2% loss; 5% loss in bursts; 0 to 30 ms more delay, in order;
everything lost."""


class NetemError(Exception):
    pass


class Netem:
    """The wrapper, run as `command` (e.g. through `sudo -n`), on `iface` for the Echo at
    `ip`."""

    def __init__(self, command: Sequence[str], iface: str, ip: str) -> None:
        self._command = tuple(command)
        self.iface = iface
        self.ip = ip

    async def apply(self, scenario: str) -> str:
        """Shape the Echo's traffic as `scenario` says, instead of as before. Returns what the
        wrapper ran, as it says. Raises `NetemError`."""
        return await self._run("apply", self.iface, self.ip, scenario)

    async def clear(self) -> str:
        """Let the Echo's traffic be. Raises `NetemError`."""
        return await self._run("clear", self.iface)

    async def _run(self, *args: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise NetemError(
                f"can't run the netem wrapper, {' '.join(self._command)}: {e}. "
                f"packages/hil/netem/install.sh installs it"
            ) from None
        out, err = await process.communicate()
        if process.returncode:
            raise NetemError(
                f"netem {' '.join(args)} failed ({process.returncode}): {err.decode().strip()}"
            )
        return out.decode()
