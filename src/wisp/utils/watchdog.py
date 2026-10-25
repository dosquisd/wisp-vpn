"""OS-level session TTL watchdog.

A deployed tunnel is billable infrastructure, and the in-app session guard can
only protect it while the Wisp process is alive. If the TUI crashes, the
terminal is killed, or the user simply forgets, a VM can keep running (and
billing) for days — the exact failure mode the orphan marker was built to
detect, but one that only triggers on the *next* launch.

This module schedules an operating-system one-shot timer that runs
``wisp destroy <provider> <region>`` when the session TTL expires. The timer
belongs to the OS, not to Wisp, so it fires even if the Wisp process died.
Manual teardown cancels it (best effort); if Wisp is gone, the timer does its
job alone.

Best effort by design: every scheduler interaction is wrapped so a missing
systemd, a locked-down schtasks, or any other platform quirk degrades to a
logged warning instead of a deploy failure.
"""

import logging
import platform
import shutil
import subprocess
import sys

from wisp.utils.platform import PlatformEnum

logger = logging.getLogger(__name__)


def get_watchdog_task_name(provider: str, region: str) -> str:
    """Return the deterministic watchdog identifier for a session target.

    Deterministic naming lets any process (the live TUI, a later CLI destroy,
    the watchdog itself) cancel the same timer without sharing state.

    Args:
        provider (str): Provider identifier (e.g. ``"aws"``).
        region (str): Deployment region identifier.

    Returns:
        str: Task/unit name, e.g. ``wisp-ttl-aws-us-east-1``.
    """
    safe_region = region.replace("/", "-")
    return f"wisp-ttl-{provider}-{safe_region}"


def _wisp_destroy_command(provider: str, region: str) -> list[str]:
    """Build the argv that runs ``wisp destroy`` for the session target.

    Prefers the installed ``wisp`` console script (absolute path, since the
    scheduler's environment may have a minimal PATH); falls back to running
    the entry point through the current interpreter.

    Args:
        provider (str): Provider identifier.
        region (str): Deployment region identifier.

    Returns:
        list[str]: The command argv.
    """
    wisp_bin = shutil.which("wisp")
    if wisp_bin:
        return [wisp_bin, "destroy", provider, region]
    # No console script on PATH: re-enter through the current interpreter and
    # seed sys.argv so argparse sees the same subcommand invocation.
    bootstrap = (
        f"import sys; sys.argv = ['wisp', 'destroy', {provider!r}, '-r', {region!r}]; "
        "from wisp.main import main; main()"
    )
    return [sys.executable, "-c", bootstrap]


def schedule_ttl_watchdog(provider: str, region: str, ttl_minutes: int) -> str | None:
    """Schedule the OS one-shot that destroys the session after the TTL.

    Args:
        provider (str): Provider identifier (e.g. ``"aws"``).
        region (str): Deployment region identifier.
        ttl_minutes (int): Lifetime cap in minutes; must be greater than zero.

    Returns:
        str | None: The scheduled task/unit name, or ``None`` when the
        platform scheduler is unavailable or refused (logged, never raised).
    """
    if ttl_minutes <= 0:
        return None

    task_name = get_watchdog_task_name(provider, region)
    # Idempotent by design: re-arming the session guard schedules the same
    # target again, and the previous transient unit/task (same name, still
    # pending) would make the new schedule fail. Cancel-first is a no-op when
    # nothing was scheduled yet.
    cancel_ttl_watchdog(provider, region)

    command = _wisp_destroy_command(provider, region)
    system = platform.system()

    try:
        if system == "Linux":
            return _schedule_systemd(task_name, command, ttl_minutes)
        if system == "Windows":
            return _schedule_schtasks(task_name, command, ttl_minutes)
        logger.debug(
            f"TTL watchdog not supported on {system}; skipping schedule "
            f"for {provider}/{region}"
        )
        return None
    except Exception as exc:
        logger.warning(f"TTL watchdog could not be scheduled ({task_name}): {exc}")
        return None


def cancel_ttl_watchdog(provider: str, region: str) -> None:
    """Cancel the watchdog for a session target (best effort).

    Called on manual teardown. Also safe to call after the watchdog already
    fired, or when nothing was ever scheduled: failures are logged at debug
    level and swallowed.

    Args:
        provider (str): Provider identifier (e.g. ``"aws"``).
        region (str): Deployment region identifier.
    """
    task_name = get_watchdog_task_name(provider, region)
    system = PlatformEnum.get_platform()
    try:
        if system == PlatformEnum.LINUX:
            # systemd-run --on-active creates TWO transient units: the .timer
            # (which fires later) and the .service it triggers. Stopping only
            # the service leaves the timer armed — a stale timer would run
            # `wisp destroy` against a future session in the same region.
            # reset-failed afterwards unloads dead units so systemd-run can
            # reuse the same name on the next deploy.
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "stop",
                    f"{task_name}.timer",
                    f"{task_name}.service",
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "reset-failed",
                    f"{task_name}.timer",
                    f"{task_name}.service",
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
        elif system == PlatformEnum.WINDOWS:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        logger.debug(f"TTL watchdog cancel attempted for {task_name}")
    except Exception as exc:
        logger.debug(f"TTL watchdog cancel skipped ({task_name}): {exc}")


def _schedule_systemd(task_name: str, command: list[str], ttl_minutes: int) -> str:
    """Schedule the one-shot via ``systemd-run --user`` (Linux).

    Args:
        task_name (str): Unit name for the transient timer/service.
        command (list[str]): Command argv to run on expiry.
        ttl_minutes (int): Lifetime cap in minutes.

    Returns:
        str: The scheduled unit name.

    Raises:
        RuntimeError: If systemd-run is missing or reports failure.
    """
    if shutil.which("systemd-run") is None:
        raise RuntimeError("systemd-run not found (no systemd user session?)")

    argv = [
        "systemd-run",
        "--user",
        f"--on-active={ttl_minutes * 60}s",
        f"--unit={task_name}",
        f"--description=Wisp session TTL watchdog ({ttl_minutes} min)",
        "--",
        *command,
    ]
    result = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"systemd-run exited {result.returncode}: {result.stderr.strip()}"
        )
    logger.info(f"TTL watchdog scheduled: {task_name} fires in {ttl_minutes} minutes")
    return task_name


def _schedule_schtasks(task_name: str, command: list[str], ttl_minutes: int) -> str:
    """Schedule the one-shot via ``schtasks`` (Windows).

    ``schtasks /SC ONCE`` runs at a wall-clock time, so the fire time is
    computed as now + TTL using the local locale's expected format.

    Args:
        task_name (str): Task name for the scheduled task.
        command (list[str]): Command argv to run on expiry.
        ttl_minutes (int): Lifetime cap in minutes.

    Returns:
        str: The scheduled task name.

    Raises:
        RuntimeError: If schtasks reports failure.
    """
    import datetime
    import locale

    fire_at = datetime.datetime.now() + datetime.timedelta(minutes=ttl_minutes)
    date_fmt = (
        locale.nl_langinfo(locale.D_FMT)
        if hasattr(locale, "nl_langinfo")
        else "%m/%d/%Y"
    )
    time_fmt = (
        locale.nl_langinfo(locale.T_FMT) if hasattr(locale, "nl_langinfo") else "%H:%M"
    )
    date_str = fire_at.strftime(date_fmt)
    time_str = fire_at.strftime(time_fmt)

    quoted_command = subprocess.list2cmdline(command)
    argv = [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        task_name,
        "/SC",
        "ONCE",
        "/SD",
        date_str,
        "/ST",
        time_str,
        "/TR",
        quoted_command,
    ]
    result = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks exited {result.returncode}: {result.stderr.strip()}"
        )
    logger.info(f"TTL watchdog scheduled: {task_name} fires at {date_str} {time_str}")
    return task_name


def watchdog_available() -> bool:
    """Report whether this platform can schedule a TTL watchdog.

    Returns:
        bool: True when the platform scheduler binary is reachable.
    """
    system = platform.system()
    if system == "Linux":
        return shutil.which("systemd-run") is not None
    if system == "Windows":
        return shutil.which("schtasks") is not None
    return False
