"""Wire protocol shared by the wisp daemon and its local client.

Messages are newline-terminated JSON objects exchanged over the Unix socket at
``SOCKET_PATH``. The unprivileged client sends a :class:`Request`; the root
daemon replies with a :class:`Response`.
"""

import enum
import json
from dataclasses import asdict, dataclass


class ActionEnum(enum.StrEnum):
    """Operations the daemon can perform on the local WireGuard interface."""

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    STATUS = "status"


@dataclass
class Request:
    """A command sent from the client to the daemon.

    Attributes:
        action (ActionEnum): The operation to perform.
        config_content (str | None): WireGuard config body; only used for the
            ``connect`` action, otherwise ``None``.
    """

    action: ActionEnum
    config_content: str | None = None  # just for "connect" action, otherwise None

    def encode(self) -> bytes:
        """Serialize to newline-terminated JSON bytes for the socket."""
        return (json.dumps(asdict(self)) + "\n").encode()

    @staticmethod
    def decode(raw: bytes) -> "Request":
        """Deserialize a request from JSON bytes."""
        return Request(**json.loads(raw.decode()))


@dataclass
class Response:
    """The daemon's reply to a :class:`Request`.

    Attributes:
        ok (bool): Whether the operation succeeded.
        message (str): Human-readable detail or command output.
        handshake_age_seconds (int | None): Age in seconds of the newest
            WireGuard handshake, or ``None`` when the local interface is down,
            has no peers, or the daemon cannot tell. The TUI uses this to
            detect that the remote end is gone.
    """

    ok: bool
    message: str = ""
    handshake_age_seconds: int | None = None

    def encode(self) -> bytes:
        """Serialize to newline-terminated JSON bytes for the socket."""
        return (json.dumps(asdict(self)) + "\n").encode()

    @staticmethod
    def decode(raw: bytes) -> "Response":
        """Deserialize a response from JSON bytes."""
        return Response(**json.loads(raw.decode()))
