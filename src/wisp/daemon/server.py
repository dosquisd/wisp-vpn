"""Privileged local daemon that controls the client-side WireGuard interface.

Runs as root under systemd (``wisp.service``, socket-activated by
``wisp.socket``). The unprivileged CLI/TUI talks to it over the Unix socket at
:data:`~wisp.daemon.protocol.SOCKET_PATH`, so it never needs ``sudo`` to bring
the local tunnel up or down.
"""

import asyncio
import subprocess
import time

from wisp.daemon.adapter import BaseAdapter, with_ownership_header
from wisp.daemon.protocol import ActionEnum, Request, Response
from wisp.daemon.transport import BaseTransport, get_transport
from wisp.utils.logger import logger

# wg-quick(8) executes these directives from the config as root, so a client
# config carrying them would hand arbitrary command execution to anyone who
# can reach the daemon socket. Wisp's client configs never use them.
FORBIDDEN_DIRECTIVES = {"preup", "postup", "predown", "postdown"}
STALE_TUNNEL_SECONDS: int = 180


def validate_client_config(content: str) -> None:
    """Reject WireGuard script hooks, which ``wg-quick`` runs as root.

    Args:
        content (str): The client configuration about to be written and
            applied by the adapter.

    Raises:
        ValueError: When any forbidden script-hook directive is present.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = stripped.split("=", 1)[0].strip().lower()
        if key in FORBIDDEN_DIRECTIVES:
            raise ValueError(f"Directive not allowed in client config: {key}")


def refuse_foreign_tunnel(adapter: BaseAdapter) -> Response | None:
    """Refuse to touch a tunnel that Wisp did not create.

    ``wg0`` is the default name for every WireGuard setup, and ``wg-quick``
    keeps its config at the same path Wisp uses. Connecting over it would
    overwrite someone else's VPN config (and ``wg-quick down`` would kill their
    tunnel first), so the daemon checks ownership before any destructive
    operation and refuses instead of guessing.

    Args:
        adapter (BaseAdapter): The platform adapter to inspect.

    Returns:
        Response | None: A rejection response when the tunnel is foreign, or
        None when Wisp may proceed.
    """
    reason = adapter.foreign_tunnel_reason()
    if reason is None:
        return None
    logger.warning(f"Refusing to operate on a foreign tunnel: {reason}")
    return Response(ok=False, message=reason)


async def handle_connect(config_content: str, adapter: BaseAdapter) -> Response:
    """Write the WireGuard config and bring the tunnel up.

    Validates ``config_content`` first (rejecting script-hook directives that
    ``wg-quick`` runs as root), refuses to touch a tunnel owned by another
    WireGuard setup, then writes the config with Wisp's ownership marker and
    brings the interface up.

    Args:
        config_content (str): The full WireGuard client configuration.
        adapter (BaseAdapter): The platform-specific adapter to use for connecting.

    Returns:
        Response: ``ok=True`` on success, otherwise the reason the config was
        rejected or the command's stderr.
    """
    try:
        validate_client_config(config_content)
    except ValueError as e:
        logger.warning(f"Rejected client config: {e}")
        return Response(ok=False, message=str(e))
    refusal = refuse_foreign_tunnel(adapter)
    if refusal is not None:
        return refusal
    try:
        adapter.connect(with_ownership_header(config_content))
        return Response(ok=True, message="connected")
    except subprocess.CalledProcessError as e:
        return Response(ok=False, message=e.stderr or str(e))


async def handle_disconnect(adapter: BaseAdapter) -> Response:
    """Bring Wisp's tunnel down, leaving a foreign one alone.

    Args:
        adapter (BaseAdapter): The platform-specific adapter to use.

    Returns:
        Response: ``ok=True`` on success, otherwise the reason nothing was
        disconnected.
    """
    refusal = refuse_foreign_tunnel(adapter)
    if refusal is not None:
        return refusal
    try:
        adapter.disconnect()
        return Response(ok=True, message="disconnected")
    except subprocess.CalledProcessError as e:
        return Response(ok=False, message=e.stderr or str(e))


async def handle_status(adapter: BaseAdapter) -> Response:
    """Return the tunnel status, its handshake age and any ownership conflict.

    A handshake age is only reported for a tunnel Wisp owns: attributing some
    other VPN's handshakes to a Wisp session would make the TUI adopt, report
    or heal the wrong tunnel. ``tunnel_conflict`` is reported regardless, so a
    caller can refuse a deployment before spending money on one.

    Args:
        adapter (BaseAdapter): The platform-specific adapter to use.

    Returns:
        Response: ``ok`` reflects the platform status exit code.
    """
    result = adapter.status()
    resp = Response(
        ok=result.returncode == 0,
        message=result.stdout,
        tunnel_conflict=adapter.foreign_tunnel_reason(),
    )
    if not adapter.is_managed():
        return resp
    try:
        age = adapter.latest_handshake_epoch()
        if age is not None and age > 0:
            resp.handshake_age_seconds = max(0, int(time.time()) - age)
    except Exception as exc:
        logger.debug(f"Failed to query handshake epoch: {exc}")
    return resp


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, adapter: BaseAdapter
) -> None:
    """Serve a single client connection: read one request, reply with one response.

    Reads a newline-delimited :class:`Request`, dispatches to the matching
    handler, and writes back a :class:`Response`. Unknown actions and unexpected
    exceptions produce an ``ok=False`` response.
    """
    try:
        raw = await reader.readline()
        req = Request.decode(raw)

        if req.action == ActionEnum.CONNECT:
            resp = await handle_connect(req.config_content or "", adapter)
        elif req.action == ActionEnum.DISCONNECT:
            resp = await handle_disconnect(adapter)
        elif req.action == ActionEnum.STATUS:
            resp = await handle_status(adapter)
        else:
            resp = Response(ok=False, message=f"unknown action: {req.action}")
    except Exception as e:
        logger.error(f"[daemon] error handling client: {e}")
        resp = Response(ok=False, message=f"internal daemon error: {e}")

    try:
        writer.write(resp.encode())
        await writer.drain()
    finally:
        writer.close()


async def reset_stale_tunnel(adapter: BaseAdapter, now: float | None = None) -> bool:
    """Tear down a stale or un-handshook WireGuard tunnel left from a previous run.

    An ephemeral tunnel must never survive a reboot or a daemon restart while
    pointing at a server that is gone, because WireGuard captures the default
    route and the local machine loses internet connectivity.

    The daemon runs as root/SYSTEM and cannot read the user's session marker,
    so the handshake age is the only reliable cross-user signal. A healthy, live
    session configured with ``PersistentKeepalive = 25`` continuously maintains
    a fresh handshake.

    Only a tunnel Wisp owns is ever torn down. ``wg0`` is also the default name
    of a user's own WireGuard setup, and dropping that would break a VPN Wisp
    never created - a reboot leaving it alone is far better than a root daemon
    killing someone else's tunnel.

    Args:
        adapter (BaseAdapter): The platform-specific adapter controlling the tunnel.
        now (float | None): Current unix timestamp, or None to use ``time.time()``.

    Returns:
        bool: True if a stale tunnel was found and torn down, False otherwise.
    """
    if not adapter.is_managed():
        logger.debug("Local tunnel is not managed by Wisp; leaving it alone")
        return False

    handshake = adapter.latest_handshake_epoch()
    if handshake is None:
        return False

    if handshake == 0:
        # No peer ever handshook: treat as stale only if the interface is up.
        try:
            status = adapter.status()
            if status.returncode != 0:
                return False
        except Exception as exc:
            logger.warning(f"Failed to check adapter status: {exc}")
            return False

        logger.warning(
            "Stale tunnel detected (interface is up but no peer has ever handshook); "
            "tearing down"
        )
        try:
            adapter.disconnect()
            return True
        except Exception as exc:
            logger.warning(f"Failed to disconnect stale tunnel: {exc}")
            return False

    current_time = time.time() if now is None else now
    if current_time - handshake > STALE_TUNNEL_SECONDS:
        logger.warning(
            f"Stale tunnel detected (last handshake {int(current_time - handshake)}s ago "
            f"> {STALE_TUNNEL_SECONDS}s); tearing down"
        )
        try:
            adapter.disconnect()
            return True
        except Exception as exc:
            logger.warning(f"Failed to disconnect stale tunnel: {exc}")
            return False

    return False


async def main(transport: BaseTransport | None = None) -> None:
    transport = transport or get_transport()
    try:
        await reset_stale_tunnel(transport.adapter)
    except Exception as exc:
        logger.warning(f"Error checking for stale tunnel on startup: {exc}")
    await transport.serve(handle_client)


if __name__ == "__main__":
    asyncio.run(main())
