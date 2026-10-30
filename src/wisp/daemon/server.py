"""Privileged local daemon that controls the client-side WireGuard interface.

Runs as root under systemd (``wisp.service``, socket-activated by
``wisp.socket``). The unprivileged CLI/TUI talks to it over the Unix socket at
:data:`~wisp.daemon.protocol.SOCKET_PATH`, so it never needs ``sudo`` to bring
the local tunnel up or down.
"""

import asyncio
import subprocess
import time

from wisp.daemon.adapter import BaseAdapter
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


async def handle_connect(config_content: str, adapter: BaseAdapter) -> Response:
    """Write the WireGuard config and bring the ``wg0`` interface up.

    Validates ``config_content`` first (rejecting script-hook directives that
    ``wg-quick`` runs as root), then writes it to :data:`WG_CONF_PATH` with
    mode ``0600`` and runs ``wg-quick up wg0``.

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
    try:
        adapter.connect(config_content)
        return Response(ok=True, message="connected")
    except subprocess.CalledProcessError as e:
        return Response(ok=False, message=e.stderr or str(e))


async def handle_disconnect(adapter: BaseAdapter) -> Response:
    """Bring the ``wg0`` interface down via ``wg-quick down wg0``."""
    try:
        adapter.disconnect()
        return Response(ok=True, message="disconnected")
    except subprocess.CalledProcessError as e:
        return Response(ok=False, message=e.stderr or str(e))


async def handle_status(adapter: BaseAdapter) -> Response:
    """Return the output of ``wg show wg0`` (``ok`` reflects the exit code)."""
    result = adapter.status()
    resp = Response(ok=result.returncode == 0, message=result.stdout)
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

    Args:
        adapter (BaseAdapter): The platform-specific adapter controlling the tunnel.
        now (float | None): Current unix timestamp, or None to use ``time.time()``.

    Returns:
        bool: True if a stale tunnel was found and torn down, False otherwise.
    """
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
