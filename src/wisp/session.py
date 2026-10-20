"""Lifecycle management for the currently active Wisp session.

Wisp creates billable cloud resources (a VM, a Pulumi stack, a local WireGuard
interface) and holds them open until it is told to stop. Historically nothing
released them on the way out, so any interruption left resources running and
costing money.

This module makes the teardown a *decision* of the session instead of a side
effect of a particular exit path:

- :class:`SessionGuard` runs the teardown exactly once, no matter how many
  triggers fire. Shutdown can be requested by a keybinding, a POSIX signal, an
  unmount, or an ``atexit`` hook, and all of them funnel into the same guard.
- :func:`write_active_session` / :func:`read_active_session` /
  :func:`clear_active_session` maintain a marker file on disk. Signals that
  cannot be caught (``SIGKILL``, a hard crash, losing power) still leave the
  marker behind, which is what lets :func:`find_orphaned_session` detect and
  clean up a previous run on the next startup.
- :func:`teardown_active_session` performs the actual release: bring the local
  WireGuard interface down, then destroy the cloud stack.

Platform notes: signal handling is POSIX-only. Windows has no ``SIGTERM``
delivery to arbitrary processes and shares console control events across the
process group, so :func:`install_signal_handlers` is a no-op there and teardown
is driven by keybindings, unmount, and ``atexit`` instead. Catching
``SIGKILL`` is impossible by design on every platform; the marker file is the
mitigation.
"""

import atexit
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from wisp.config.constants import WISP_ACTIVE_SESSION_PATH
from wisp.config.credentials import VPSCredentials
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.utils import logger, secure_file
from wisp.utils.watchdog import cancel_ttl_watchdog
from wisp.wireguard import disconnect_wireguard_client

# Teardown callable: receives a short human-readable reason for the shutdown.
TeardownCallback = Callable[[str], None]


class ActiveSession(dict):
    """Marker describing the tunnel currently running (or left behind).

    Behaves as a plain dict (``provider``, ``region``, ``pid``, ``started_at``)
    so it serializes without a custom encoder.
    """

    @property
    def provider(self) -> str:
        """Provider name owning the session (e.g. ``"aws"``)."""
        return str(self.get("provider", ""))

    @property
    def region(self) -> str:
        """Region the session's resources live in."""
        return str(self.get("region", ""))

    @property
    def pid(self) -> int:
        """PID of the process that created the session."""
        try:
            return int(self.get("pid", 0))
        except (TypeError, ValueError):  # fmt: skip
            return 0

    @property
    def started_at(self) -> float:
        """Unix timestamp of when the session was created."""
        try:
            return float(self.get("started_at", 0.0))
        except (TypeError, ValueError):  # fmt: skip
            return 0.0

    @property
    def uptime_seconds(self) -> float:
        """Seconds elapsed since the session was created."""
        if not self.started_at:
            return 0.0
        return max(0.0, time.time() - self.started_at)

    @property
    def vps(self) -> dict:
        """VPS connection details captured at deploy time (empty if absent).

        Present only for ``vps`` sessions deployed after this field existed;
        older markers (and non-VPS markers) return an empty dict, and the
        destroy paths fall back to the TOML ``[vps]`` section.
        """
        value = self.get("vps")
        return dict(value) if isinstance(value, dict) else {}

    def summary(self) -> str:
        """Return a one-line, human-readable description of the session."""
        minutes, seconds = divmod(int(self.uptime_seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return (
            f"{self.provider.upper()}/{self.region} "
            f"up {hours:d}h{minutes:02d}m{seconds:02d}s (pid {self.pid})"
        )


def _process_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` currently exists.

    Args:
        pid (int): Process ID to probe.

    Returns:
        bool: True when the process exists and is signalable.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill on Windows can terminate the target, so it must not be used
        # as a probe. Ask the kernel for the exit code instead.
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user.
        return True
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    """Return True if a Windows process with ``pid`` is still running.

    Uses ``PROCESS_QUERY_LIMITED_INFORMATION`` plus ``GetExitCodeProcess``
    rather than ``os.kill``, which on Windows terminates the target process
    instead of probing it.

    Args:
        pid (int): Process ID to probe.

    Returns:
        bool: True when the process exists and has not exited.
    """
    import ctypes

    still_active = 259
    query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def write_active_session(provider: str, region: str, vps: dict | None = None) -> None:
    """Record the active tunnel in the marker file.

    Args:
        provider (str): Provider name owning the session.
        region (str): Region the resources live in.
        vps (dict | None): VPS-only connection details used for the deploy
            (``host``, ``ssh_user``, ``ssh_port``, ``ssh_key_path``) so a
            later destroy tears down the right server without depending on
            the TOML ``[vps]`` section.
    """
    payload = {
        "provider": provider,
        "region": region,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    if vps:
        payload["vps"] = vps
    try:
        WISP_ACTIVE_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        WISP_ACTIVE_SESSION_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        secure_file(WISP_ACTIVE_SESSION_PATH)
    except OSError as exc:
        # The marker is a best-effort safety net; never fail a deploy over it.
        logger.warning(f"Could not write the active session marker: {exc}")


def read_active_session() -> ActiveSession | None:
    """Read the marker file, if present and well-formed.

    Returns:
        ActiveSession | None: The recorded session, or None when absent/corrupt.
    """
    if not WISP_ACTIVE_SESSION_PATH.is_file():
        return None
    try:
        data: Any = json.loads(WISP_ACTIVE_SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Ignoring unreadable active session marker: {exc}")
        return None
    if not isinstance(data, dict):
        return None
    return ActiveSession(data)


def clear_active_session() -> None:
    """Remove the marker file if it exists."""
    try:
        WISP_ACTIVE_SESSION_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"Could not remove the active session marker: {exc}")


def find_orphaned_session() -> ActiveSession | None:
    """Return a leftover session whose owning process is gone.

    A marker whose PID is still alive belongs to a Wisp instance running right
    now (possibly this one), so it is not an orphan.

    Returns:
        ActiveSession | None: The orphaned session, or None when there is none.
    """
    session = read_active_session()
    if session is None:
        return None
    if session.pid == os.getpid() or _process_alive(session.pid):
        return None
    return session


class SessionGuard:
    """Runs a session's teardown exactly once, whatever triggers it.

    Keybindings, POSIX signals, unmounts and ``atexit`` all call
    :meth:`shutdown`. The first call wins; later calls return False without
    repeating the teardown, which keeps the operation idempotent even when
    several triggers fire at once (for example ``Ctrl+C`` followed by ``SIGTERM``
    as the process exits).
    """

    def __init__(self) -> None:
        """Initialize an unarmed guard with no teardown attached."""
        self._teardown: TeardownCallback | None = None
        self._lock = threading.Lock()
        self._finished = False
        self._reason: str = ""
        self._last_error: str | None = None

    @property
    def armed(self) -> bool:
        """True when a teardown is attached and has not run yet."""
        with self._lock:
            return self._teardown is not None and not self._finished

    @property
    def reason(self) -> str:
        """Reason reported by the shutdown that actually ran."""
        with self._lock:
            return self._reason

    def arm(self, teardown: TeardownCallback) -> None:
        """Attach the teardown to run on shutdown.

        Args:
            teardown (TeardownCallback): Callable invoked with a short reason.
        """
        with self._lock:
            if self._finished:
                return
            self._teardown = teardown

    def shutdown(self, reason: str) -> bool:
        """Run the teardown once, if it has not run yet.

        Args:
            reason (str): Short explanation, logged and passed to the teardown.

        Returns:
            bool: True if this call ran the teardown cleanly (marker cleared),
                False if it was a no-op (already shut down, nothing armed) or
                the teardown raised — see :attr:`last_error` for the cause.
        """
        with self._lock:
            if self._finished or self._teardown is None:
                return False
            self._finished = True
            self._reason = reason
            teardown = self._teardown

        logger.info(f"Session shutdown requested ({reason}); releasing resources")
        try:
            teardown(reason)
        except Exception as exc:
            # A failed teardown must not be swallowed silently: the resources
            # are probably still running, and the marker is left in place so
            # the next run can offer to clean up.
            self._last_error = str(exc)
            logger.error(f"Session teardown failed ({reason}): {exc}")
            return False
        clear_active_session()
        logger.info("Session resources released")
        return True

    @property
    def last_error(self) -> str | None:
        """Error message from the last failed teardown, if any."""
        return self._last_error


def _destroy_provider(provider_name: str):
    """Build the provider instance used by a teardown, honoring the marker.

    VPS sessions carry their deploy-time connection details in the marker:
    the TOML ``[vps]`` section may never have had them (CLI-flag deploys) or
    may have drifted since. Cloud providers need no special handling.

    Args:
        provider_name (str): Provider name owning the session.

    Returns:
        BaseProvider: The provider instance for ``delete_vm``.
    """
    provider_enum = ProviderEnum(provider_name)
    if provider_enum == ProviderEnum.VPS:
        marker = read_active_session()
        marker_vps = marker.vps if marker is not None else {}
        if marker_vps.get("host"):
            return PROVIDERS_MAP[provider_enum](
                credentials=VPSCredentials(**marker_vps)
            )
    return PROVIDERS_MAP[provider_enum]()


def teardown_active_session(
    provider_name: str,
    region: str,
    on_progress: Callable[[str, float | None], None] | None = None,
) -> None:
    """Release a session's resources: local tunnel first, then the cloud stack.

    The WireGuard interface is brought down before the VM is destroyed so the
    client does not keep probing a host that is about to disappear. If the
    daemon is unreachable the cloud teardown still runs, because a missing local
    daemon is not a reason to leave a paid VM running.

    Args:
        provider_name (str): Provider name owning the session.
        region (str): Region whose resources should be destroyed.
        on_progress (Callable[[str, float | None], None] | None): Optional
            progress callback matching
            :data:`~wisp.providers.base.ProgressCallback`.

    Raises:
        Exception: Whatever the provider's ``delete_vm`` raises, after the local
            tunnel has been attempted.
    """

    def report(message: str, progress: float | None = None) -> None:
        if on_progress is not None:
            on_progress(message, progress)

    # Cancel the optional OS-level TTL watchdog first: teardown is happening
    # right now, so the scheduled `wisp destroy` must not fire later. Best
    # effort — a missed cancel is harmless (the destroy is idempotent).
    try:
        cancel_ttl_watchdog(provider_name, region)
    except Exception as exc:
        logger.debug(f"TTL watchdog cancel skipped: {exc}")

    report("Disconnecting the local tunnel...", 0.1)
    try:
        response = disconnect_wireguard_client()
        if not response.ok:
            logger.warning(f"Could not bring the local tunnel down: {response.message}")
    except Exception as exc:
        logger.warning(f"Local tunnel teardown skipped: {exc}")

    report("Destroying cloud resources...", 0.4)
    provider = _destroy_provider(provider_name)
    provider.delete_vm(region=region, on_progress=on_progress)

    # The marker is the single source of truth for "there are resources out
    # there". Reaching this line means they are gone, so the marker must follow
    # suit — otherwise the menu keeps offering to clean up an orphan whose cloud
    # resources no longer exist. A raise above (failed delete_vm) deliberately
    # skips this, so the next run can still detect and offer the cleanup.
    clear_active_session()


def _leave_alternate_screen() -> None:
    """Best-effort restoration of a terminal that is about to disappear.

    Signals such as ``SIGHUP`` arrive with the terminal already gone or going
    away, so the process must not block on restoring it, and must still reset
    whatever session it can reach.
    """
    try:
        sys.stdout.write("\x1b[?1049l\x1b[0m")
        sys.stdout.flush()
    except Exception:
        pass


def _exit_after_signal(signum: int) -> None:
    """Leave the process after a signal-driven teardown.

    Installing a handler replaces the default action, so once the handler
    returns the process would keep running with a destroyed tunnel: an
    interactive shell under a long-dead ``kill`` is worse than exiting.
    ``os._exit`` is used deliberately, because the usual ``sys.exit`` path would
    unwind into the event loop that is mid-signal-handler.

    Args:
        signum (int): The signal number received, used to build the exit status
            following the shell convention of ``128 + signum``.
    """
    _leave_alternate_screen()
    try:
        os._exit(128 + int(signum))
    except (OverflowError, TypeError, ValueError):  # fmt: skip
        os._exit(1)


def install_signal_handlers(guard: SessionGuard) -> bool:
    """Route terminal and termination signals into the guard.

    Textual's driver disables ``ISIG`` on the terminal, so ``Ctrl+C`` arrives
    as a key event and is handled by a binding rather than here. These handlers
    cover everything delivered from outside the TUI:

    - ``SIGHUP``: the terminal window was closed. Without this, closing the
      terminal would leave the VM running with no way to reach it.
    - ``SIGTERM``: sent by ``kill``, a supervisor, or a session manager.
    - ``SIGINT``: sent explicitly (``kill -INT``) or by a shell job control.

    Args:
        guard (SessionGuard): Guard to notify on a received signal.

    Returns:
        bool: True when at least one handler was installed, False on platforms
            without POSIX signal delivery (Windows).
    """
    if not hasattr(signal, "SIGTERM"):
        logger.debug("Signal handling unavailable on this platform")
        return False

    def handle(signum: int, _frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        logger.warning(f"Received {name}; releasing the active session")
        guard.shutdown(f"received {name}")
        _exit_after_signal(signum)

    handled = False
    for sig in (
        getattr(signal, "SIGHUP", None),
        signal.SIGTERM,
        signal.SIGINT,
    ):
        if sig is None:
            continue
        try:
            signal.signal(sig, handle)
            handled = True
        except (OSError, ValueError) as exc:
            # Handlers can only be installed from the main thread of the main
            # interpreter; a worker thread must keep the default behavior.
            logger.debug(f"Could not install handler for {sig}: {exc}")
            return handled
    return handled


def register_atexit(guard: SessionGuard) -> None:
    """Run the guard's teardown on normal interpreter exit.

    Covers the exit paths that never reach an unmount hook, including Windows.
    The guard is idempotent, so this composes safely with signals and unmounts.

    Args:
        guard (SessionGuard): Guard to notify at exit.
    """
    atexit.register(lambda: guard.shutdown("interpreter exit"))
