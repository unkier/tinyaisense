"""`tinyaisense-hub`: run the hub, and the owner's commands."""

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import datetime
from typing import Any

import uvloop
from tinyaisense_wire import MDNS_SERVICE

from .config import (
    ConfigError,
    config_path,
    load_config,
    new_config,
    runtime_dir,
    state_dir,
    write_config,
)
from .errors import HubError
from .hub import Hub
from .models import MODELS, ModelError, fetch, models_dir
from .owner import SOCKET_NAME, OwnerClient
from .state import StateError

PROG = "tinyaisense-hub"

_log = logging.getLogger("tinyaisense_hub")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ConfigError, StateError, HubError, ModelError, OSError) as e:
        return _fail(str(e))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="The tinyaisense hub.")
    commands = parser.add_subparsers(required=True, metavar="command")

    init = commands.add_parser("init", help="write a new config with a random agent token")
    init.set_defaults(run=_init)

    serve = commands.add_parser("serve", help="run the hub")
    serve.add_argument(
        "--auto-approve", action="store_true", help="pair new devices without asking (dev only)"
    )
    serve.add_argument(
        "--no-mdns",
        action="store_true",
        help="don't advertise over mDNS; devices then need the hub's address set",
    )
    serve.set_defaults(run=_serve)

    devices = commands.add_parser("devices", help="list paired and pending devices")
    devices.set_defaults(run=_devices)

    approve = commands.add_parser(
        "approve", help="pair a pending device", usage=f"{PROG} approve <device> [as <name>]"
    )
    approve.add_argument("device", help="a device_id, or a unique prefix of one")
    approve.add_argument("as_name", nargs="*", metavar="as <name>")
    approve.set_defaults(run=_approve, parser=approve)

    rename = commands.add_parser("rename", help="rename a device; it reconnects")
    rename.add_argument("device", help="its name or device_id")
    rename.add_argument("name")
    rename.set_defaults(run=_rename)

    forget = commands.add_parser("forget", help="unpair a device; it must pair again")
    forget.add_argument("device", help="its name or device_id")
    forget.set_defaults(run=_forget)

    identify = commands.add_parser("identify", help="blink a device white, to tell it apart")
    identify.add_argument("device", help="its name or device_id")
    identify.add_argument("seconds", nargs="?", type=int, help="1 to 300 (default 5)")
    identify.set_defaults(run=_identify)

    diag = commands.add_parser("diag", help="show a device's health")
    diag.add_argument("device", help="its name or device_id")
    diag.set_defaults(run=_diag)

    volume = commands.add_parser("volume", help="set a device's speaker volume")
    volume.add_argument("device", help="its name or device_id")
    volume.add_argument("level", type=int, help="0 (mute) to 100")
    volume.set_defaults(run=_volume)

    stats = commands.add_parser("stats", help="show the button-to-agent latency histogram")
    stats.set_defaults(run=_stats)

    models = commands.add_parser("models", help="the local speech engines' models")
    models_commands = models.add_subparsers(required=True, metavar="command")
    fetch = models_commands.add_parser(
        "fetch", help="download the models the local engines run, checked by sha256"
    )
    fetch.set_defaults(run=_models_fetch)
    return parser


def _init(args: argparse.Namespace) -> int:
    path = config_path()
    try:
        write_config(path, new_config())
    except FileExistsError:
        return _fail(f"{path} already exists; delete it first to start over")
    print(f"wrote {path}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    config = load_config(config_path())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    async def serve() -> None:
        hub = Hub(
            config,
            state_dir=state_dir(),
            runtime_dir=runtime_dir(),
            auto_approve=args.auto_approve,
            mdns=None if args.no_mdns else MDNS_SERVICE,
        )
        async with hub:
            _log.info(
                "serving devices on port %d (TCP), %d (UDP); owner socket %s",
                hub.device_address[1],
                hub.udp_port,
                hub.owner_socket,
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()
            _log.info("shutting down")

    run_hub(serve())
    return 0


def run_hub[T](main: Coroutine[Any, Any, T]) -> T:
    """Run the hub's loop: uvloop, with a 1 ms GIL switch interval instead of 5 ms, so that
    a thread holding the GIL delays the loop by 1 ms at most."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(0.001)
    try:
        return uvloop.run(main)
    finally:
        sys.setswitchinterval(previous)


def _models_fetch(args: argparse.Namespace) -> int:
    into = models_dir()
    fetch(MODELS, into, report=print)
    print(f"every model is in {into}")
    return 0


def _devices(args: argparse.Namespace) -> int:
    devices = _owner(lambda owner: owner.devices())
    rows = [("DEVICE ID", "NAME", "STATE", "LAST SEEN", "BOARD", "FIRMWARE")]
    rows += [
        (
            d["device_id"],
            d["name"] or "-",
            "pending" if d["pending"] else "online" if d["online"] else "offline",
            "now" if d["online"] else _local_time(d["last_seen"]),
            d["board"],
            d["firmware"],
        )
        for d in devices
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )
    return 0


def _local_time(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _approve(args: argparse.Namespace) -> int:
    rest: list[str] = args.as_name
    if rest and (len(rest) != 2 or rest[0] != "as"):
        parser: argparse.ArgumentParser = args.parser
        parser.error("expected `approve <device> [as <name>]`")
    name = rest[1] if rest else None
    approved = _owner(lambda owner: owner.approve(args.device, name))
    print(f"approved {approved['device_id']} as {approved['name']}")
    return 0


def _rename(args: argparse.Namespace) -> int:
    renamed = _owner(lambda owner: owner.rename(args.device, args.name))
    print(f"renamed {renamed['device_id']} to {renamed['name']}")
    return 0


def _forget(args: argparse.Namespace) -> int:
    _owner(lambda owner: owner.forget(args.device))
    print(f"forgot {args.device}")
    return 0


def _identify(args: argparse.Namespace) -> int:
    identified = _owner(lambda owner: owner.identify(args.device, args.seconds))
    print(f"{identified['name']} is blinking white")
    return 0


_DIAG_UNITS = {"heap_free": " B", "heap_min": " B", "rssi": " dBm"}


def _diag(args: argparse.Namespace) -> int:
    diag = _owner(lambda owner: owner.diag(args.device))
    width = max(map(len, diag))
    for key, value in diag.items():
        if key == "stack_hwm":
            value = " ".join(f"{task}={free}" for task, free in value.items())
        print(f"{key.ljust(width)}  {value}{_DIAG_UNITS.get(key, '')}")
    return 0


def _volume(args: argparse.Namespace) -> int:
    volume = _owner(lambda owner: owner.set_volume(args.device, args.level))
    if volume["applied"]:
        print(f"{volume['name']}: volume {args.level}")
    else:
        print(f"{volume['name']} is offline; it gets volume {args.level} when it's back")
    return 0


_BAR = 40
"""The width of the histogram's longest bar."""


def _stats(args: argparse.Namespace) -> int:
    latency = _owner(lambda owner: owner.stats())["button_latency"]
    if not latency["count"]:
        print("button to agent socket: no edges yet")
        return 0
    print(
        f"button to agent socket: {latency['count']} edges, p50 {latency['p50_ms']} ms, "
        f"p99 {latency['p99_ms']} ms, max {latency['max_ms']} ms"
    )
    buckets = latency["buckets"]
    used = [i for i, bucket in enumerate(buckets) if bucket["count"]]
    labels = [
        f">{buckets[i - 1]['le_ms']}" if bucket["le_ms"] is None else f"<={bucket['le_ms']}"
        for i, bucket in enumerate(buckets)
    ]
    width = max(len(label) for label in labels[used[0] : used[-1] + 1])
    most = max(bucket["count"] for bucket in buckets)
    for i in range(used[0], used[-1] + 1):
        count = buckets[i]["count"]
        bar = "#" * round(count / most * _BAR)
        print(f"{labels[i].rjust(width)} ms  {bar.ljust(_BAR)}  {count}")
    return 0


def _owner[T](call: Callable[[OwnerClient], Awaitable[T]]) -> T:
    async def run() -> T:
        try:
            async with OwnerClient(runtime_dir() / SOCKET_NAME) as owner:
                return await call(owner)
        except FileNotFoundError, ConnectionRefusedError:
            raise HubError("no_hub", f"no hub is running; start one with `{PROG} serve`") from None

    return asyncio.run(run())


def _fail(message: str) -> int:
    print(f"{PROG}: {message}", file=sys.stderr)
    return 1
