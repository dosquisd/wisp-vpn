"""Read-only diagnostic checks for Wisp prerequisites.

Inspects the local machine for missing dependencies, permissions, daemon socket,
WireGuard tools, Pulumi CLI/backend, provider credentials, TTL watchdog,
active/orphaned session markers, and directory writability.

A check ``FAIL``s only when the missing piece blocks every deployment: the
daemon, the WireGuard tools, or the runtime directories. Capabilities that
belong to a single provider (Pulumi, per-provider credentials) ``WARN``
instead, because which provider to deploy to is the user's decision: a rejected
AWS key must not fail a machine that uses Custom VPS, and vice versa.
"""

from __future__ import annotations

import enum
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from wisp.config.constants import (
    WIREGUARD_KEYS_DIR,
    WISP_CONFIG_DIR,
    WISP_DATA_DIR,
    WISP_LOG_DIR,
)
from wisp.config.credentials import (
    AWSCredentials,
    GCPCredentials,
    OCICredentials,
    resolve_aws_credentials,
    resolve_gcp_credentials,
    resolve_oci_credentials,
    resolve_vps_credentials,
    validate_aws_credentials,
    validate_gcp_credentials,
    validate_oci_credentials,
)
from wisp.config.settings import load_toml_config, load_wisp_config
from wisp.session import process_alive, read_active_session
from wisp.utils.privileges import root_invocation
from wisp.wireguard.local_client import status_wireguard_client


class CheckStatus(enum.StrEnum):
    """Possible outcomes for a diagnostic check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Check:
    """A single prerequisite verification result.

    Attributes:
        name: Short, stable identifier for the check (e.g. ``daemon``).
        status: The check outcome (:class:`CheckStatus`).
        detail: Human-readable explanation of the result.
        hint: Actionable command, path, or env var to resolve warnings/failures.
            Empty for ``OK`` and ``SKIP``.
    """

    name: str
    status: CheckStatus
    detail: str
    hint: str = ""


_TIMEOUT_SENTINEL = object()


def _run_bounded[T](func: Callable[[], T], timeout: float) -> T | object:
    """Execute ``func`` in a daemon thread, bounding execution time to ``timeout``.

    Daemon threads never delay interpreter shutdown, preventing hanging exits
    if a remote API or socket blocks indefinitely.

    Args:
        func: Zero-argument callable to invoke.
        timeout: Maximum time in seconds to wait for completion.

    Returns:
        The result of ``func()``, or ``_TIMEOUT_SENTINEL`` on timeout.

    Raises:
        BaseException: Re-raises any exception raised within the thread.
    """
    result_holder: list[T] = []
    error_holder: list[BaseException] = []

    def target() -> None:
        try:
            result_holder.append(func())
        except BaseException as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        return _TIMEOUT_SENTINEL
    if error_holder:
        raise error_holder[0]
    return result_holder[0] if result_holder else None


def _is_windows() -> bool:
    """Return True if running on Windows."""
    return sys.platform == "win32"


def _windows_service_name() -> str:
    """Resolve the Windows service name from the daemon service wrapper."""
    try:
        from wisp.daemon.windows_service import WispService

        return getattr(WispService, "_svc_name_", "WispService")
    except ImportError:
        return "WispService"


def _safe_check(name: str, check_fn: Callable[[], Check]) -> Check:
    """Execute ``check_fn`` and convert unexpected exceptions into a FAIL Check.

    Args:
        name: Name of the check being run.
        check_fn: Zero-argument function returning a :class:`Check`.

    Returns:
        The produced :class:`Check`, or a FAIL check if an unhandled exception
        occurred.
    """
    try:
        return check_fn()
    except Exception as exc:
        return Check(
            name=name,
            status=CheckStatus.FAIL,
            detail=f"unexpected error: {exc}",
            hint="check command output and error logs",
        )


_DAEMON_PROBE_TIMEOUT_SECONDS = 3.0
_DAEMON_RESTART_HINT = "sudo systemctl restart wisp.service wisp.socket"


def _daemon_check_from_probe(probe: object, restart_hint: str) -> Check:
    """Turn a daemon status probe into a :class:`Check`.

    A decoded ``Response`` proves the daemon answered, so its ``ok`` field says
    nothing about daemon health: ``ok=False`` only reflects the platform status
    command's exit code, which is exactly what happens when no tunnel is up.
    Only a missing answer (exception or timeout) means the daemon is
    unreachable.

    Args:
        probe: Result of the bounded ``status_wireguard_client`` call.
        restart_hint: Platform-specific command that restarts the daemon.

    Returns:
        Check: Reachability outcome, warning on a tunnel conflict or an
        internal daemon error.
    """
    if probe is _TIMEOUT_SENTINEL:
        return Check(
            name="daemon",
            status=CheckStatus.FAIL,
            detail=(
                f"daemon did not answer within {_DAEMON_PROBE_TIMEOUT_SECONDS:.0f}s "
                "(unreachable)"
            ),
            hint=restart_hint,
        )

    if not hasattr(probe, "ok"):
        return Check(
            name="daemon",
            status=CheckStatus.FAIL,
            detail="unexpected response from daemon",
            hint=restart_hint,
        )

    message = (getattr(probe, "message", "") or "").strip()
    conflict = getattr(probe, "tunnel_conflict", None)

    if message.startswith("internal daemon error") or message.startswith(
        "unknown action"
    ):
        return Check(
            name="daemon",
            status=CheckStatus.WARN,
            detail=f"daemon answered with an error: {message}",
            hint=restart_hint,
        )

    if conflict:
        return Check(
            name="daemon",
            status=CheckStatus.WARN,
            detail=f"daemon reachable; tunnel conflict: {conflict}",
            hint="wisp disconnect",
        )

    if getattr(probe, "ok", False):
        return Check(
            name="daemon",
            status=CheckStatus.OK,
            detail=message or "daemon reachable, tunnel is up",
            hint="",
        )

    return Check(
        name="daemon",
        status=CheckStatus.OK,
        detail="daemon reachable, no tunnel is up",
        hint="",
    )


def check_daemon() -> Check:
    """Verify daemon socket (Linux) or service (Windows) accessibility."""
    if _is_windows():
        service_name = _windows_service_name()
        try:
            res = subprocess.run(
                ["sc.exe", "query", service_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0 or service_name.lower() not in res.stdout.lower():
                return Check(
                    name="daemon",
                    status=CheckStatus.FAIL,
                    detail=f"Windows service '{service_name}' not installed or not running",
                    hint="run installer as Administrator (setup.ps1)",
                )
        except Exception as exc:
            return Check(
                name="daemon",
                status=CheckStatus.FAIL,
                detail=f"failed to query service '{service_name}': {exc}",
                hint="run installer as Administrator (setup.ps1)",
            )

        probe = _run_bounded(
            status_wireguard_client, timeout=_DAEMON_PROBE_TIMEOUT_SECONDS
        )
        return _daemon_check_from_probe(probe, f"Restart-Service {service_name}")

    # Linux implementation
    socket_path = Path("/run/wisp.sock")
    try:
        st = os.stat(socket_path)
    except FileNotFoundError:
        return Check(
            name="daemon",
            status=CheckStatus.FAIL,
            detail=f"daemon socket not found at {socket_path}",
            hint=root_invocation("daemon install"),
        )
    except OSError as exc:
        return Check(
            name="daemon",
            status=CheckStatus.FAIL,
            detail=f"could not stat {socket_path}: {exc}",
            hint=root_invocation("daemon install"),
        )

    if not stat.S_ISSOCK(st.st_mode):
        mode = st.st_mode
        file_type = (
            "directory"
            if stat.S_ISDIR(mode)
            else "regular file"
            if stat.S_ISREG(mode)
            else f"mode {oct(mode)}"
        )
        return Check(
            name="daemon",
            status=CheckStatus.FAIL,
            detail=f"path exists but is not a socket: {socket_path} ({file_type})",
            hint=root_invocation("daemon install"),
        )

    try:
        probe = _run_bounded(
            status_wireguard_client, timeout=_DAEMON_PROBE_TIMEOUT_SECONDS
        )
    except Exception as exc:
        return Check(
            name="daemon",
            status=CheckStatus.FAIL,
            detail=str(exc) or type(exc).__name__,
            hint=_DAEMON_RESTART_HINT,
        )

    return _daemon_check_from_probe(probe, _DAEMON_RESTART_HINT)


def check_membership() -> Check:
    """Verify group membership (Linux) or Administrator token (Windows)."""
    if _is_windows():
        try:
            res = subprocess.run(
                ["whoami", "/groups"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "S-1-5-32-544" in res.stdout:
                return Check(
                    name="membership",
                    status=CheckStatus.OK,
                    detail="current token has Administrator privileges (S-1-5-32-544)",
                    hint="",
                )
            return Check(
                name="membership",
                status=CheckStatus.WARN,
                detail="current token lacks Administrator privileges (S-1-5-32-544)",
                hint="run terminal as Administrator (named pipe requires Administrator rights)",
            )
        except Exception as exc:
            return Check(
                name="membership",
                status=CheckStatus.WARN,
                detail=f"could not check Administrator group: {exc}",
                hint="run terminal as Administrator (named pipe requires Administrator rights)",
            )

    try:
        import grp

        group = grp.getgrnam("wisp")
    except KeyError:
        return Check(
            name="membership",
            status=CheckStatus.FAIL,
            detail="group 'wisp' does not exist",
            hint=root_invocation("daemon install"),
        )
    except Exception as exc:
        return Check(
            name="membership",
            status=CheckStatus.FAIL,
            detail=f"failed to lookup group 'wisp': {exc}",
            hint=root_invocation("daemon install"),
        )

    user_groups = set(os.getgroups())
    user_groups.add(os.getgid())

    if group.gr_gid in user_groups:
        return Check(
            name="membership",
            status=CheckStatus.OK,
            detail=f"user is member of 'wisp' group (gid {group.gr_gid})",
            hint="",
        )
    return Check(
        name="membership",
        status=CheckStatus.WARN,
        detail=f"user is not in 'wisp' group (gid {group.gr_gid}) or membership is not active in this session",
        hint="sudo usermod -aG wisp $USER and log out/in (or run 'newgrp wisp')",
    )


def check_wireguard_tools() -> Check:
    """Check for local WireGuard utilities (wg/wg-quick or wireguard.exe)."""
    if _is_windows():
        exe = shutil.which("wireguard")
        std_dir = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WireGuard"
        )
        std_exe = std_dir / "wireguard.exe"
        if exe:
            return Check(
                name="wireguard-tools",
                status=CheckStatus.OK,
                detail=f"found {exe}",
                hint="",
            )
        if std_exe.is_file():
            return Check(
                name="wireguard-tools",
                status=CheckStatus.OK,
                detail=f"found {std_exe}",
                hint="",
            )
        return Check(
            name="wireguard-tools",
            status=CheckStatus.FAIL,
            detail="wireguard.exe not found on PATH or under %ProgramFiles%\\WireGuard",
            hint="winget install WireGuard.WireGuard or run setup.ps1",
        )

    wg = shutil.which("wg")
    wg_quick = shutil.which("wg-quick")
    if wg and wg_quick:
        return Check(
            name="wireguard-tools",
            status=CheckStatus.OK,
            detail=f"found {wg} and {wg_quick}",
            hint="",
        )

    missing: list[str] = []
    if not wg:
        missing.append("wg")
    if not wg_quick:
        missing.append("wg-quick")
    return Check(
        name="wireguard-tools",
        status=CheckStatus.FAIL,
        detail=f"missing wireguard tools: {', '.join(missing)}",
        hint="sudo apt install wireguard",
    )


def check_pulumi() -> Check:
    """Verify Pulumi CLI installation and backend authentication.

    Pulumi is only needed by the Pulumi-backed providers (AWS, OCI, GCP), so a
    missing or unauthenticated CLI is a warning: a Custom VPS deployment never
    touches it, and provider choice is the user's.
    """
    pulumi_bin = shutil.which("pulumi")
    if pulumi_bin:
        try:
            res = subprocess.run(
                [pulumi_bin, "whoami"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                user = res.stdout.strip()
                return Check(
                    name="pulumi",
                    status=CheckStatus.OK,
                    detail=f"authenticated as {user}",
                    hint="",
                )
            err = (res.stderr or res.stdout).strip() or f"exit code {res.returncode}"
            return Check(
                name="pulumi",
                status=CheckStatus.WARN,
                detail=f"pulumi whoami failed: {err}",
                hint="pulumi login",
            )
        except subprocess.TimeoutExpired:
            return Check(
                name="pulumi",
                status=CheckStatus.WARN,
                detail="pulumi whoami timed out after 15s",
                hint="pulumi login",
            )
        except Exception as exc:
            return Check(
                name="pulumi",
                status=CheckStatus.WARN,
                detail=f"failed to run pulumi whoami: {exc}",
                hint="pulumi login",
            )

    candidate = (
        Path.home() / ".pulumi" / "bin" / ("pulumi.exe" if _is_windows() else "pulumi")
    )
    if candidate.is_file():
        return Check(
            name="pulumi",
            status=CheckStatus.WARN,
            detail=f"Pulumi is installed at {candidate} but not on PATH",
            hint="add ~/.pulumi/bin to PATH (required for scheduled TTL destroy)",
        )
    return Check(
        name="pulumi",
        status=CheckStatus.WARN,
        detail="pulumi not found (only AWS/OCI/GCP need it; Custom VPS does not)",
        hint="curl -fsSL https://get.pulumi.com | sh",
    )


def _is_aws_configured(creds: AWSCredentials) -> bool:
    """Determine whether AWS credentials or configuration exist."""
    if (
        creds.access_key_id
        or creds.secret_access_key
        or creds.session_token
        or creds.profile
    ):
        return True
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return True
    from wisp.providers.aws.constants import DEFAULT_AWS_REGION

    toml = load_toml_config().get("aws", {})
    if toml.get("region") or toml.get("profile"):
        return True
    if (Path.home() / ".aws" / "credentials").is_file() or (
        Path.home() / ".aws" / "config"
    ).is_file():
        return True
    if creds.region and creds.region != DEFAULT_AWS_REGION:
        return True
    return False


def check_credentials_aws() -> Check:
    """Validate AWS credentials against STS GetCallerIdentity.

    Provider credentials are never fatal: configuring several providers is
    normal, and one rejected set must not fail a machine whose other providers
    work. The check warns and exits 0.
    """
    creds = resolve_aws_credentials()
    if not _is_aws_configured(creds):
        return Check(
            name="credentials-aws",
            status=CheckStatus.SKIP,
            detail="not configured (skipped)",
            hint="",
        )

    try:
        res = _run_bounded(lambda: validate_aws_credentials(creds), timeout=5.0)
    except Exception as exc:
        return Check(
            name="credentials-aws",
            status=CheckStatus.WARN,
            detail=f"AWS validation error: {exc}",
            hint="aws configure or set AWS environment variables",
        )

    if res is _TIMEOUT_SENTINEL:
        return Check(
            name="credentials-aws",
            status=CheckStatus.WARN,
            detail="credential validation timed out after 5.0s",
            hint="check AWS connectivity and credentials (aws configure)",
        )

    if res is True:
        return Check(
            name="credentials-aws",
            status=CheckStatus.OK,
            detail=f"valid AWS credentials (region: {creds.region})",
            hint="",
        )
    return Check(
        name="credentials-aws",
        status=CheckStatus.WARN,
        detail="AWS credentials rejected (STS GetCallerIdentity failed)",
        hint="aws configure or set AWS environment variables",
    )


def _is_oci_configured(creds: OCICredentials) -> bool:
    """Determine whether OCI credentials or configuration exist."""
    if creds.is_explicitly_configured():
        return True
    if (Path.home() / ".oci" / "config").is_file():
        return True
    if os.environ.get("OCI_TENANCY") or os.environ.get("OCI_USER"):
        return True
    return False


def check_credentials_oci() -> Check:
    """Validate OCI credentials against Identity API.

    Warnings only: a rejected OCI credential set must not fail a machine that
    uses another provider.
    """
    creds = resolve_oci_credentials()
    if not _is_oci_configured(creds):
        return Check(
            name="credentials-oci",
            status=CheckStatus.SKIP,
            detail="not configured (skipped)",
            hint="",
        )

    try:
        res = _run_bounded(lambda: validate_oci_credentials(creds), timeout=5.0)
    except Exception as exc:
        return Check(
            name="credentials-oci",
            status=CheckStatus.WARN,
            detail=f"OCI validation error: {exc}",
            hint="check ~/.oci/config profile and [oci].compartment_ocid in wisp.toml",
        )

    if res is _TIMEOUT_SENTINEL:
        return Check(
            name="credentials-oci",
            status=CheckStatus.WARN,
            detail="credential validation timed out after 5.0s",
            hint="check OCI connectivity and credentials (~/.oci/config)",
        )

    if res is True:
        return Check(
            name="credentials-oci",
            status=CheckStatus.OK,
            detail=f"valid OCI credentials (region: {creds.region})",
            hint="",
        )
    return Check(
        name="credentials-oci",
        status=CheckStatus.WARN,
        detail="OCI credentials rejected (list_region_subscriptions failed)",
        hint="check ~/.oci/config profile and [oci].compartment_ocid in wisp.toml",
    )


def _is_gcp_configured(creds: GCPCredentials) -> bool:
    """Determine whether GCP credentials or project ID exist."""
    if creds.project_id or creds.credentials_path:
        return True
    if (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    ):
        return True
    adc_file = (
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    )
    if adc_file.is_file():
        return True
    toml = load_toml_config().get("gcp", {})
    if toml.get("project_id") or toml.get("credentials_path"):
        return True
    return False


def check_credentials_gcp() -> Check:
    """Validate GCP credentials against Compute RegionsClient.

    Warnings only, for the same reason as the other providers: authorization to
    deploy is per-provider, not a machine-wide prerequisite.
    """
    creds = resolve_gcp_credentials()
    if not _is_gcp_configured(creds):
        return Check(
            name="credentials-gcp",
            status=CheckStatus.SKIP,
            detail="not configured (skipped)",
            hint="",
        )

    try:
        res = _run_bounded(lambda: validate_gcp_credentials(creds), timeout=5.0)
    except Exception as exc:
        return Check(
            name="credentials-gcp",
            status=CheckStatus.WARN,
            detail=f"GCP validation error: {exc}",
            hint="set [gcp].credentials_path to a service-account JSON or run gcloud auth application-default login",
        )

    if res is _TIMEOUT_SENTINEL:
        return Check(
            name="credentials-gcp",
            status=CheckStatus.WARN,
            detail="credential validation timed out after 5.0s",
            hint="check GCP connectivity and credentials",
        )

    if res is True:
        return Check(
            name="credentials-gcp",
            status=CheckStatus.OK,
            detail=f"valid GCP credentials (project: {creds.project_id}, region: {creds.region})",
            hint="",
        )
    return Check(
        name="credentials-gcp",
        status=CheckStatus.WARN,
        detail=f"GCP credentials rejected for project '{creds.project_id}'",
        hint="set [gcp].credentials_path to a service-account JSON or run gcloud auth application-default login",
    )


def check_credentials_vps() -> Check:
    """Validate Custom VPS connection details.

    Warnings only: a broken VPS entry must not fail a machine that deploys to a
    cloud provider instead.
    """
    creds = resolve_vps_credentials()
    if not creds.is_explicitly_configured():
        return Check(
            name="credentials-vps",
            status=CheckStatus.SKIP,
            detail="not configured (skipped)",
            hint="",
        )

    if not creds.ssh_key_path:
        return Check(
            name="credentials-vps",
            status=CheckStatus.WARN,
            detail="SSH key path not configured",
            hint="configure [vps].host and [vps].ssh_key_path in wisp.toml and add public key to server's authorized_keys",
        )

    key_file = Path(creds.ssh_key_path).expanduser()
    if not key_file.is_file():
        return Check(
            name="credentials-vps",
            status=CheckStatus.WARN,
            detail=f"SSH key not found at '{creds.ssh_key_path}'",
            hint="configure [vps].host and [vps].ssh_key_path in wisp.toml and add public key to server's authorized_keys",
        )

    return Check(
        name="credentials-vps",
        status=CheckStatus.OK,
        detail=f"host {creds.host}, user {creds.ssh_user}, port {creds.ssh_port}, key {creds.ssh_key_path}",
        hint="",
    )


def check_ttl_watchdog() -> Check:
    """Verify local OS scheduler availability for the session TTL watchdog."""
    cfg = load_wisp_config()
    ttl = cfg.max_session_ttl_minutes
    if ttl <= 0:
        return Check(
            name="ttl-watchdog",
            status=CheckStatus.SKIP,
            detail="local TTL backstop disabled",
            hint="",
        )

    scheduler_cmd = "schtasks" if _is_windows() else "systemd-run"
    scheduler_path = shutil.which(scheduler_cmd)
    if scheduler_path:
        return Check(
            name="ttl-watchdog",
            status=CheckStatus.OK,
            detail=f"scheduler available ({scheduler_path}), TTL cap {ttl} min",
            hint="",
        )
    return Check(
        name="ttl-watchdog",
        status=CheckStatus.WARN,
        detail=f"OS scheduler '{scheduler_cmd}' not found on PATH; local TTL backstop unavailable",
        hint="set [general].max_session_ttl_minutes = 0 in wisp.toml to disable it explicitly",
    )


def check_session_marker() -> Check:
    """Inspect the active session marker for resumable, expired, or orphaned sessions."""
    session = read_active_session()
    if session is None:
        return Check(
            name="session-marker",
            status=CheckStatus.SKIP,
            detail="no active session",
            hint="",
        )

    destroy_hint = f"wisp destroy {session.provider} -r {session.region}"
    if session.is_expired():
        return Check(
            name="session-marker",
            status=CheckStatus.WARN,
            detail=f"active session marker has expired ({session.summary()})",
            hint=destroy_hint,
        )

    if session.pid != os.getpid() and not process_alive(session.pid):
        return Check(
            name="session-marker",
            status=CheckStatus.WARN,
            detail=f"orphaned session marker found; owning process {session.pid} is dead ({session.summary()})",
            hint=destroy_hint,
        )

    return Check(
        name="session-marker",
        status=CheckStatus.OK,
        detail=f"resumable session: {session.summary()}",
        hint="",
    )


def check_directories() -> Check:
    """Verify writability of runtime data, config, log, and key directories."""
    dirs_to_check = [
        WISP_DATA_DIR,
        WISP_CONFIG_DIR,
        WISP_LOG_DIR,
        WIREGUARD_KEYS_DIR,
    ]
    unwritable: list[Path] = []
    for path in dirs_to_check:
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        if not (os.access(target, os.W_OK) and os.access(target, os.X_OK)):
            unwritable.append(path)

    if unwritable:
        return Check(
            name="directories",
            status=CheckStatus.FAIL,
            detail=f"unwritable runtime directories: {', '.join(str(p) for p in unwritable)}",
            hint="check directory permissions/chown or override via WISP_DATA_DIR, WISP_CONFIG_DIR, WISP_LOG_DIR",
        )

    return Check(
        name="directories",
        status=CheckStatus.OK,
        detail=f"all runtime directories writable ({WISP_DATA_DIR}, {WISP_CONFIG_DIR}, {WISP_LOG_DIR})",
        hint="",
    )


def run_doctor() -> list[Check]:
    """Execute all diagnostic checks in a stable order.

    Returns:
        list[Check]: Complete sequence of diagnostic check results.
    """
    return [
        _safe_check("daemon", check_daemon),
        _safe_check("membership", check_membership),
        _safe_check("wireguard-tools", check_wireguard_tools),
        _safe_check("pulumi", check_pulumi),
        _safe_check("credentials-aws", check_credentials_aws),
        _safe_check("credentials-oci", check_credentials_oci),
        _safe_check("credentials-gcp", check_credentials_gcp),
        _safe_check("credentials-vps", check_credentials_vps),
        _safe_check("ttl-watchdog", check_ttl_watchdog),
        _safe_check("session-marker", check_session_marker),
        _safe_check("directories", check_directories),
    ]


def render_report(checks: Sequence[Check]) -> str:
    """Format check results as a human-readable plain text report.

    Args:
        checks: Sequence of check results to render.

    Returns:
        str: Aligned report with status tags, details, hints, and summary counts.
    """
    lines = ["Wisp doctor", ""]
    ok_count = 0
    warn_count = 0
    fail_count = 0
    skip_count = 0

    for c in checks:
        if c.status == CheckStatus.OK:
            ok_count += 1
        elif c.status == CheckStatus.WARN:
            warn_count += 1
        elif c.status == CheckStatus.FAIL:
            fail_count += 1
        elif c.status == CheckStatus.SKIP:
            skip_count += 1

        tag = f"[{c.status.value}]".ljust(6)
        lines.append(f"{tag} {c.name:<17} {c.detail}")
        if c.hint:
            lines.append(f"       hint: {c.hint}")

    lines.append("")
    warn_label = "warning" if warn_count == 1 else "warnings"
    lines.append(
        f"{ok_count} ok, {warn_count} {warn_label}, {fail_count} failed, {skip_count} skipped"
    )
    if fail_count > 0:
        lines.append("Fix the failures above to ensure Wisp works properly.")

    return "\n".join(lines) + "\n"


def run_doctor_command() -> int:
    """Run all diagnostic checks, print the report, and return the exit code.

    Returns:
        int: 1 if any check failed, 0 if all checks passed, warned, or skipped.
    """
    checks = run_doctor()
    report = render_report(checks)
    print(report, end="")
    if any(c.status == CheckStatus.FAIL for c in checks):
        return 1
    return 0
