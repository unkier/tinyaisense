from typing import Any, Literal

type HubErrorCode = Literal[
    # Owner requests.
    "not_found",
    "ambiguous",
    "invalid_name",
    "name_taken",
    # Starting or reaching the hub.
    "already_running",
    "unsafe_runtime_dir",
    "no_hub",
    # The owner socket itself.
    "bad_request",
    "unknown_method",
    "invalid_params",
    # Agent requests: docs/protocol/agent.md#errors.
    "unauthorized",
    "unsupported",
    "invalid_args",
    "unknown_address",
    "busy",
    "timeout",
    "disconnected",
    "overflow",
    # Both.
    "failed",
]


class HubError(Exception):
    """A request the hub refuses. `data` adds fields to the error, e.g. who holds the hub."""

    def __init__(self, code: HubErrorCode, message: str, **data: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code: HubErrorCode = code
        self.message = message
        self.data = data
