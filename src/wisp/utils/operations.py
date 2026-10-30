"""Teardown signaling between Wisp processes.

A Wisp tunnel is billable infrastructure that several processes can attempt to
mutate: an interactive Textual TUI, manual CLI invocations, and the OS-scheduled
watchdog that runs ``wisp destroy`` when a session TTL expires.

Mutual exclusion is NOT implemented here. Pulumi already owns it: every stack
update takes a backend lock, and a second concurrent update raises
:class:`pulumi.automation.errors.ConcurrentUpdateError` instead of corrupting
state. Re-implementing that lock would duplicate a guarantee we get for free and
add a failure mode of our own (a stale lock file outliving a crashed process).

What Pulumi's lock does NOT give us is visibility: a running TUI cannot observe
another process's update through it. This module provides that missing signal
with a small, disposable file:

- :func:`begin_teardown` records that a destroy is underway, so a TUI showing a
  live tunnel can tell the difference between "the user is destroying this" and
  "a scheduled task is destroying this right now".
- :func:`read_teardown_notice` lets that TUI poll for it without any IPC
  channel, socket, or shared event loop.
- :func:`end_teardown` clears it when the destroy finishes.

The notice is advisory, never authoritative: nothing waits on it, nothing is
serialized by it, and a missing notice never blocks an operation.
"""

import json
import os
import time

from wisp.config.constants import WISP_DATA_DIR
from wisp.session import process_alive
from wisp.utils import logger, secure_file

WISP_TEARDOWN_NOTICE_FILE_NAME: str = "teardown-notice.json"
WISP_TEARDOWN_NOTICE_PATH = WISP_DATA_DIR / WISP_TEARDOWN_NOTICE_FILE_NAME

# A notice older than this is treated as abandoned: the process that wrote it
# either died or was killed mid-destroy.
STALE_NOTICE_SECONDS: int = 1800


def begin_teardown(provider: str, region: str) -> None:
    """Record a teardown notice indicating a destroy operation is in progress.

    Best effort: a notice that cannot be written only costs observability, so
    an ``OSError`` is logged at debug level and swallowed.

    Args:
        provider (str): Provider owning the session being destroyed.
        region (str): Region where resources are being destroyed.
    """
    path = WISP_TEARDOWN_NOTICE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "provider": provider,
            "region": region,
            "pid": os.getpid(),
            "started_at": time.time(),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        secure_file(path)
    except OSError as exc:
        logger.debug(f"Could not write teardown notice: {exc}")


def end_teardown() -> None:
    """Remove the teardown notice file if owned by the current process.

    Best effort: any ``OSError`` is swallowed and logged at debug level. The
    ownership check keeps one process from clearing another's notice.
    """
    path = WISP_TEARDOWN_NOTICE_PATH
    try:
        if not path.is_file():
            return
        content = path.read_text(encoding="utf-8").strip()
        if content:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("pid") == os.getpid():
                path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug(f"Could not remove teardown notice: {exc}")
    except json.JSONDecodeError:
        pass


def read_teardown_notice(now: float | None = None) -> dict | None:
    """Read the active teardown notice, if present and fresh.

    Never raises: the TUI calls this once per second, so any malformed or
    half-written file must degrade to "no notice" rather than break the view.

    Args:
        now (float | None): Current unix timestamp, or ``None`` for
            ``time.time()``. Injectable so the staleness rule is testable.

    Returns:
        dict | None: The notice payload when a live process is tearing a
        session down, or ``None`` when the file is missing, unreadable,
        references a dead PID, or is older than :data:`STALE_NOTICE_SECONDS`.
    """
    path = WISP_TEARDOWN_NOTICE_PATH
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
    except (OSError, json.JSONDecodeError):
        return None

    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return None

    if pid <= 0 or not process_alive(pid):
        return None

    try:
        started_at = float(data.get("started_at", 0.0))
    except (TypeError, ValueError):
        return None

    current_time = time.time() if now is None else now
    if current_time - started_at > STALE_NOTICE_SECONDS:
        return None

    return data
