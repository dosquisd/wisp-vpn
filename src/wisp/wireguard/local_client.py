"""Local WireGuard client: thin wrapper that drives the privileged daemon.

Each function sends a single :class:`~wisp.daemon.protocol.Request` to the daemon
over the Unix socket and returns its :class:`~wisp.daemon.protocol.Response`.
"""

from wisp.daemon.protocol import ActionEnum, Request, Response
from wisp.daemon.transport import get_transport


def connect_wireguard_client(config_content: str) -> Response:
    """Ask the daemon to write the config and bring ``wg0`` up.

    Args:
        config_content (str): The full WireGuard client configuration.
    """
    transport = get_transport()
    return transport.send(
        Request(action=ActionEnum.CONNECT, config_content=config_content)
    )


def disconnect_wireguard_client() -> Response:
    """Ask the daemon to bring the ``wg0`` interface down."""
    transport = get_transport()
    return transport.send(Request(action=ActionEnum.DISCONNECT))


def status_wireguard_client() -> Response:
    """Ask the daemon for the current ``wg0`` status."""
    transport = get_transport()
    return transport.send(Request(action=ActionEnum.STATUS))


def tunnel_conflict() -> str | None:
    """Return why the local tunnel name is unusable, or ``None`` when it is free.

    Callers use this before provisioning: ``wg0`` is the default name of every
    WireGuard setup, and discovering the conflict only after a VM exists would
    leave billable resources behind for a predictable local condition.

    Returns:
        str | None: The daemon's explanation when another WireGuard setup owns
        the tunnel name, or ``None`` when Wisp may proceed (including when the
        daemon is unreachable, which the caller handles separately).
    """
    try:
        return status_wireguard_client().tunnel_conflict
    except Exception:
        return None
