"""The `tinyaisense` command."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit

PROG = "tinyaisense"
TOKEN_ENV = "TINYAISENSE_TOKEN"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description="The tinyaisense agent SDK.")
    commands = parser.add_subparsers(dest="command", required=True)
    mcp = commands.add_parser(
        "mcp",
        help="serve the hub's MCP tools over stdio, in a Session the shim holds",
        description="Serve the hub's MCP tools over stdio, for MCP clients that can't reach "
        "its /mcp over Streamable HTTP. The shim holds a Session over the agent API, and "
        "exits once it ends.",
    )
    mcp.add_argument("--hub", required=True, help="the hub's URL, e.g. http://hub.local:8421")
    mcp.add_argument("--token", help=f"the agent token (default: env {TOKEN_ENV})")
    mcp.add_argument(
        "--takeover",
        action="store_true",
        help="take the hub from the agent that holds it, if any, instead of exiting",
    )
    mcp.add_argument(
        "--channel",
        action="store_true",
        help="push what the human says by push-to-talk, and button gestures, into Claude "
        "Code as channel events",
    )
    args = parser.parse_args(argv)
    hub: str = args.hub
    token: str | None = args.token or os.environ.get(TOKEN_ENV)
    if not token:
        parser.error(f"no agent token: pass --token or set {TOKEN_ENV}")
    if urlsplit(hub).scheme not in ("http", "https"):
        parser.error(f"--hub takes the hub's http:// or https:// URL, not {hub!r}")
    try:
        from .mcp_shim import ShimError, run
    except ModuleNotFoundError as e:
        if e.name is None or e.name.partition(".")[0] != "mcp":
            raise
        return _fail(
            "the MCP shim needs the mcp extra: pip install 'tinyaisense[mcp]'", f"{PROG} mcp"
        )
    try:
        asyncio.run(run(hub, token, takeover=args.takeover, channel=args.channel))
    except ShimError as e:
        return _fail(str(e), f"{PROG} mcp")
    return 0


def _fail(message: str, prog: str = PROG) -> int:
    print(f"{prog}: {message}", file=sys.stderr)
    return 1
