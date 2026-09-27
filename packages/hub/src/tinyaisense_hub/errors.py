from typing import Literal

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
    "failed",
]


class HubError(Exception):
    """A request the hub refuses."""

    def __init__(self, code: HubErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code: HubErrorCode = code
        self.message = message
