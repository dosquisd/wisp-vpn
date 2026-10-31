"""Unit tests for the wisp doctor diagnostic checks and reporting."""

from __future__ import annotations

import collections
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

import wisp.doctor as doctor
from wisp.daemon.protocol import Response
from wisp.doctor import (
    Check,
    CheckStatus,
    check_daemon,
    check_directories,
    check_membership,
    check_pulumi,
    check_session_marker,
    check_ttl_watchdog,
    check_wireguard_tools,
    render_report,
    run_doctor,
    run_doctor_command,
)
from wisp.main import CommandEnum, parse_args
from wisp.session import ActiveSession


def _intercept_stat(
    target_path: str,
    exc: Exception | None = None,
    result: os.stat_result | None = None,
):
    orig_stat = os.stat

    def _stat(path, *args, **kwargs):
        if str(path) == target_path:
            if exc:
                raise exc
            if result:
                return result
        return orig_stat(path, *args, **kwargs)

    return _stat


# ─── DAEMON CHECK TESTS ──────────────────────────────────────────


def test_daemon_socket_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", exc=FileNotFoundError("no such file")),
    )

    c = check_daemon()
    assert c.status == CheckStatus.FAIL
    assert "daemon socket not found" in c.detail
    assert "daemon install" in c.hint


def test_daemon_socket_not_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )

    c = check_daemon()
    assert c.status == CheckStatus.FAIL
    assert "is not a socket" in c.detail
    assert "regular file" in c.detail
    assert "daemon install" in c.hint


def test_daemon_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )
    monkeypatch.setattr(
        doctor,
        "status_wireguard_client",
        lambda: Response(ok=True, message="interface is up"),
    )

    c = check_daemon()
    assert c.status == CheckStatus.OK
    assert "interface is up" in c.detail
    assert c.hint == ""


def test_daemon_probe_conflict_downgrades_to_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )
    monkeypatch.setattr(
        doctor,
        "status_wireguard_client",
        lambda: Response(
            ok=True,
            message="interface is up",
            tunnel_conflict="owned by another VPN",
        ),
    )

    c = check_daemon()
    assert c.status == CheckStatus.WARN
    assert "tunnel conflict: owned by another VPN" in c.detail
    assert c.hint == "wisp disconnect"


def test_daemon_probe_no_tunnel_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down interface is a normal state, not a broken daemon.

    ``handle_status`` sets ``ok`` from the platform status command's exit code,
    so ``Response(ok=False, message="")`` is what a reachable daemon returns
    whenever no tunnel is up (``wg show wg0`` fails with empty stdout).
    """
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )
    monkeypatch.setattr(
        doctor,
        "status_wireguard_client",
        lambda: Response(ok=False, message=""),
    )

    c = check_daemon()
    assert c.status == CheckStatus.OK
    assert "no tunnel is up" in c.detail
    assert c.hint == ""


def test_daemon_internal_error_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )
    monkeypatch.setattr(
        doctor,
        "status_wireguard_client",
        lambda: Response(ok=False, message="internal daemon error: boom"),
    )

    c = check_daemon()
    assert c.status == CheckStatus.WARN
    assert "boom" in c.detail
    assert "systemctl restart" in c.hint


def test_daemon_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )

    def _raise():
        raise PermissionError("permission denied on /run/wisp.sock")

    monkeypatch.setattr(doctor, "status_wireguard_client", _raise)

    c = check_daemon()
    assert c.status == CheckStatus.FAIL
    assert "permission denied" in c.detail
    assert "systemctl restart wisp.service wisp.socket" in c.hint


def test_daemon_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_stat = os.stat_result((stat.S_IFSOCK | 0o660, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", result=fake_stat),
    )
    monkeypatch.setattr(
        doctor, "_run_bounded", lambda *a, **kw: doctor._TIMEOUT_SENTINEL
    )

    c = check_daemon()
    assert c.status == CheckStatus.FAIL
    assert "did not answer" in c.detail


def test_daemon_windows_service_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=["sc.exe"], returncode=1060, stdout="", stderr="FAILED"
        ),
    )

    c = check_daemon()
    assert c.status == CheckStatus.FAIL
    assert "not installed" in c.detail
    assert "setup.ps1" in c.hint


def test_daemon_windows_service_present_probe_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=["sc.exe"],
            returncode=0,
            stdout="SERVICE_NAME: WispService\nSTATE: RUNNING",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "status_wireguard_client",
        lambda: Response(ok=True, message="running"),
    )

    c = check_daemon()
    assert c.status == CheckStatus.OK
    assert "running" in c.detail


# ─── MEMBERSHIP CHECK TESTS ──────────────────────────────────────


def test_membership_group_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_grp = types.ModuleType("grp")

    def _raise_key_error(name):
        raise KeyError(name)

    fake_grp.getgrnam = _raise_key_error  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "grp", fake_grp)

    c = check_membership()
    assert c.status == CheckStatus.FAIL
    assert "group 'wisp' does not exist" in c.detail
    assert "daemon install" in c.hint


def test_membership_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_grp = types.ModuleType("grp")
    GroupEntry = collections.namedtuple("GroupEntry", ["gr_name", "gr_gid"])
    fake_grp.getgrnam = lambda name: GroupEntry(gr_name="wisp", gr_gid=1234)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "grp", fake_grp)
    monkeypatch.setattr(os, "getgroups", lambda: [1000, 1234])
    monkeypatch.setattr(os, "getgid", lambda: 1000)

    c = check_membership()
    assert c.status == CheckStatus.OK
    assert "user is member of 'wisp' group (gid 1234)" in c.detail
    assert c.hint == ""


def test_membership_primary_gid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_grp = types.ModuleType("grp")
    GroupEntry = collections.namedtuple("GroupEntry", ["gr_name", "gr_gid"])
    fake_grp.getgrnam = lambda name: GroupEntry(gr_name="wisp", gr_gid=1234)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "grp", fake_grp)
    monkeypatch.setattr(os, "getgroups", lambda: [1000])  # not in getgroups
    monkeypatch.setattr(os, "getgid", lambda: 1234)  # but is primary gid

    c = check_membership()
    assert c.status == CheckStatus.OK
    assert "gid 1234" in c.detail
    assert c.hint == ""


def test_membership_non_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    fake_grp = types.ModuleType("grp")
    GroupEntry = collections.namedtuple("GroupEntry", ["gr_name", "gr_gid"])
    fake_grp.getgrnam = lambda name: GroupEntry(gr_name="wisp", gr_gid=1234)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "grp", fake_grp)
    monkeypatch.setattr(os, "getgroups", lambda: [1000])
    monkeypatch.setattr(os, "getgid", lambda: 1000)

    c = check_membership()
    assert c.status == CheckStatus.WARN
    assert "user is not in 'wisp' group" in c.detail
    assert "usermod -aG wisp" in c.hint


def test_membership_windows_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=["whoami", "/groups"],
            returncode=0,
            stdout="BUILTIN\\Administrators Group S-1-5-32-544 Enabled",
        ),
    )

    c = check_membership()
    assert c.status == CheckStatus.OK
    assert "Administrator privileges" in c.detail


def test_membership_windows_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=["whoami", "/groups"],
            returncode=0,
            stdout="Mandatory Label\\Medium Mandatory Level",
        ),
    )

    c = check_membership()
    assert c.status == CheckStatus.WARN
    assert "lacks Administrator privileges" in c.detail
    assert "run terminal as Administrator" in c.hint


# ─── WIREGUARD TOOLS CHECK TESTS ─────────────────────────────────


def test_wireguard_tools_linux_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)

    def _which(cmd: str):
        if cmd == "wg":
            return "/usr/bin/wg"
        if cmd == "wg-quick":
            return "/usr/bin/wg-quick"
        return None

    monkeypatch.setattr(doctor.shutil, "which", _which)

    c = check_wireguard_tools()
    assert c.status == CheckStatus.OK
    assert "/usr/bin/wg" in c.detail
    assert "/usr/bin/wg-quick" in c.detail


def test_wireguard_tools_linux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)

    def _which(cmd: str):
        if cmd == "wg":
            return "/usr/bin/wg"
        return None

    monkeypatch.setattr(doctor.shutil, "which", _which)

    c = check_wireguard_tools()
    assert c.status == CheckStatus.FAIL
    assert "missing wireguard tools: wg-quick" in c.detail
    assert "apt install wireguard" in c.hint


def test_wireguard_tools_windows_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda cmd: (
            r"C:\Program Files\WireGuard\wireguard.exe" if cmd == "wireguard" else None
        ),
    )

    c = check_wireguard_tools()
    assert c.status == CheckStatus.OK
    assert "wireguard.exe" in c.detail


def test_wireguard_tools_windows_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_is_windows", lambda: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda _: False)

    c = check_wireguard_tools()
    assert c.status == CheckStatus.FAIL
    assert "wireguard.exe not found" in c.detail
    assert "winget install WireGuard.WireGuard" in c.hint


# ─── PULUMI CHECK TESTS ──────────────────────────────────────────


def test_pulumi_on_path_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: "/usr/local/bin/pulumi")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=["pulumi", "whoami"], returncode=0, stdout="testuser\n"
        ),
    )

    c = check_pulumi()
    assert c.status == CheckStatus.OK
    assert "authenticated as testuser" in c.detail
    assert c.hint == ""


def test_pulumi_whoami_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: "/usr/local/bin/pulumi")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=["pulumi", "whoami"],
            returncode=1,
            stdout="",
            stderr="error: not logged in",
        ),
    )

    c = check_pulumi()
    assert c.status == CheckStatus.WARN
    assert "error: not logged in" in c.detail
    assert c.hint == "pulumi login"


def test_pulumi_whoami_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: "/usr/local/bin/pulumi")

    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["pulumi", "whoami"], timeout=15)

    monkeypatch.setattr(subprocess, "run", _timeout)

    c = check_pulumi()
    assert c.status == CheckStatus.WARN
    assert "timed out after 15s" in c.detail
    assert c.hint == "pulumi login"


def test_pulumi_not_on_path_but_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)

    orig_is_file = Path.is_file

    def _is_file(self: Path):
        if ".pulumi" in str(self) and self.name == "pulumi":
            return True
        return orig_is_file(self)

    monkeypatch.setattr(Path, "is_file", _is_file)

    c = check_pulumi()
    assert c.status == CheckStatus.WARN
    assert "installed at" in c.detail
    assert "but not on PATH" in c.detail
    assert "add ~/.pulumi/bin to PATH" in c.hint


def test_pulumi_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    orig_is_file = Path.is_file

    def _is_file(self: Path):
        if ".pulumi" in str(self) and self.name in ("pulumi", "pulumi.exe"):
            return False
        return orig_is_file(self)

    monkeypatch.setattr(Path, "is_file", _is_file)

    c = check_pulumi()
    assert c.status == CheckStatus.WARN
    assert "pulumi not found" in c.detail
    assert "curl -fsSL https://get.pulumi.com | sh" in c.hint


# ─── PROVIDER CREDENTIALS TESTS ──────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "check_fn", "resolve_name", "validate_name", "creds_cls"),
    [
        (
            "aws",
            doctor.check_credentials_aws,
            "resolve_aws_credentials",
            "validate_aws_credentials",
            doctor.AWSCredentials,
        ),
        (
            "oci",
            doctor.check_credentials_oci,
            "resolve_oci_credentials",
            "validate_oci_credentials",
            doctor.OCICredentials,
        ),
        (
            "gcp",
            doctor.check_credentials_gcp,
            "resolve_gcp_credentials",
            "validate_gcp_credentials",
            doctor.GCPCredentials,
        ),
    ],
)
def test_cloud_provider_states(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    check_fn,
    resolve_name: str,
    validate_name: str,
    creds_cls,
) -> None:
    # 1. Unconfigured -> SKIP
    monkeypatch.setattr(doctor, f"_is_{provider}_configured", lambda _: False)
    c = check_fn()
    assert c.status == CheckStatus.SKIP
    assert "not configured" in c.detail
    assert c.hint == ""

    # 2. Configured + valid -> OK
    monkeypatch.setattr(doctor, f"_is_{provider}_configured", lambda _: True)
    monkeypatch.setattr(
        doctor,
        resolve_name,
        lambda: (
            creds_cls(region="test-region")
            if hasattr(creds_cls, "region")
            else creds_cls()
        ),
    )
    monkeypatch.setattr(doctor, validate_name, lambda _: True)
    c = check_fn()
    assert c.status == CheckStatus.OK
    assert "valid" in c.detail
    assert c.hint == ""

    # 3. Configured + invalid -> WARN with hint (never fatal: see doctor module docstring)
    monkeypatch.setattr(doctor, validate_name, lambda _: False)
    c = check_fn()
    assert c.status == CheckStatus.WARN
    assert "rejected" in c.detail
    assert c.hint != ""

    # 4. Probe timeout -> WARN
    monkeypatch.setattr(
        doctor, "_run_bounded", lambda *a, **kw: doctor._TIMEOUT_SENTINEL
    )
    c = check_fn()
    assert c.status == CheckStatus.WARN
    assert "timed out after 5.0s" in c.detail
    assert c.hint != ""


def test_vps_credentials_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from wisp.config.credentials import VPSCredentials

    # 1. Unconfigured -> SKIP
    monkeypatch.setattr(
        doctor,
        "resolve_vps_credentials",
        lambda: VPSCredentials(host=""),
    )
    c = doctor.check_credentials_vps()
    assert c.status == CheckStatus.SKIP

    # 2. Configured without key path -> WARN
    monkeypatch.setattr(
        doctor,
        "resolve_vps_credentials",
        lambda: VPSCredentials(host="192.168.1.1", ssh_key_path=""),
    )
    c = doctor.check_credentials_vps()
    assert c.status == CheckStatus.WARN
    assert "SSH key path not configured" in c.detail

    # 3. Configured with nonexistent key -> WARN
    non_existent = tmp_path / "nonexistent.pem"
    monkeypatch.setattr(
        doctor,
        "resolve_vps_credentials",
        lambda: VPSCredentials(host="192.168.1.1", ssh_key_path=str(non_existent)),
    )
    c = doctor.check_credentials_vps()
    assert c.status == CheckStatus.WARN
    assert "SSH key not found" in c.detail

    # 4. Configured with valid key file -> OK
    key_file = tmp_path / "id_rsa"
    key_file.write_text("fake key")
    monkeypatch.setattr(
        doctor,
        "resolve_vps_credentials",
        lambda: VPSCredentials(host="192.168.1.1", ssh_key_path=str(key_file)),
    )
    c = doctor.check_credentials_vps()
    assert c.status == CheckStatus.OK
    assert "host 192.168.1.1" in c.detail


# ─── TTL WATCHDOG TESTS ──────────────────────────────────────────


def test_ttl_watchdog_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from wisp.config.settings import WispConfig

    monkeypatch.setattr(
        doctor,
        "load_wisp_config",
        lambda: WispConfig(max_session_ttl_minutes=0),
    )

    c = check_ttl_watchdog()
    assert c.status == CheckStatus.SKIP
    assert "disabled" in c.detail


def test_ttl_watchdog_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from wisp.config.settings import WispConfig

    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    monkeypatch.setattr(
        doctor,
        "load_wisp_config",
        lambda: WispConfig(max_session_ttl_minutes=60),
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: "/usr/bin/systemd-run")

    c = check_ttl_watchdog()
    assert c.status == CheckStatus.OK
    assert "/usr/bin/systemd-run" in c.detail
    assert "TTL cap 60 min" in c.detail


def test_ttl_watchdog_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from wisp.config.settings import WispConfig

    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    monkeypatch.setattr(
        doctor,
        "load_wisp_config",
        lambda: WispConfig(max_session_ttl_minutes=60),
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)

    c = check_ttl_watchdog()
    assert c.status == CheckStatus.WARN
    assert "systemd-run' not found" in c.detail
    assert "[general].max_session_ttl_minutes = 0" in c.hint


# ─── SESSION MARKER TESTS ────────────────────────────────────────


def test_session_marker_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "read_active_session", lambda: None)

    c = check_session_marker()
    assert c.status == CheckStatus.SKIP
    assert "no active session" in c.detail


def test_session_marker_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    sess = ActiveSession(
        {
            "provider": "aws",
            "region": "us-east-1",
            "pid": os.getpid(),
            "started_at": 1000.0,
            "ttl_minutes": 60,
            "expires_at": 9999999999.0,
        }
    )
    monkeypatch.setattr(doctor, "read_active_session", lambda: sess)
    monkeypatch.setattr(doctor, "process_alive", lambda _: True)

    c = check_session_marker()
    assert c.status == CheckStatus.OK
    assert "resumable session" in c.detail


def test_session_marker_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    sess = ActiveSession(
        {
            "provider": "aws",
            "region": "us-east-1",
            "pid": os.getpid(),
            "started_at": 1000.0,
            "ttl_minutes": 10,
            "expires_at": 1600.0,
        }
    )
    monkeypatch.setattr(doctor, "read_active_session", lambda: sess)

    c = check_session_marker()
    assert c.status == CheckStatus.WARN
    assert "marker has expired" in c.detail
    assert c.hint == "wisp destroy aws -r us-east-1"


def test_session_marker_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    sess = ActiveSession(
        {
            "provider": "gcp",
            "region": "europe-west1",
            "pid": 999999,
            "started_at": 1000.0,
            "ttl_minutes": 0,
            "expires_at": 0.0,
        }
    )
    monkeypatch.setattr(doctor, "read_active_session", lambda: sess)
    monkeypatch.setattr(doctor, "process_alive", lambda _: False)

    c = check_session_marker()
    assert c.status == CheckStatus.WARN
    assert "orphaned session marker" in c.detail
    assert c.hint == "wisp destroy gcp -r europe-west1"


# ─── DIRECTORIES TESTS ───────────────────────────────────────────


def test_directories_writable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"
    keys_dir = data_dir / "keys"

    monkeypatch.setattr(doctor, "WISP_DATA_DIR", data_dir)
    monkeypatch.setattr(doctor, "WISP_CONFIG_DIR", config_dir)
    monkeypatch.setattr(doctor, "WISP_LOG_DIR", log_dir)
    monkeypatch.setattr(doctor, "WIREGUARD_KEYS_DIR", keys_dir)

    # Directories do NOT exist yet; nearest ancestor is tmp_path, which is writable
    c = check_directories()
    assert c.status == CheckStatus.OK
    assert "all runtime directories writable" in c.detail
    # Must NOT have created the directories
    assert not data_dir.exists()
    assert not config_dir.exists()


def test_directories_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(doctor, "WISP_DATA_DIR", data_dir)
    monkeypatch.setattr(doctor, "WISP_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(doctor, "WISP_LOG_DIR", tmp_path)
    monkeypatch.setattr(doctor, "WIREGUARD_KEYS_DIR", tmp_path)

    orig_access = os.access

    def _fake_access(path, mode):
        if str(path) == str(tmp_path):
            return False
        return orig_access(path, mode)

    monkeypatch.setattr(os, "access", _fake_access)

    c = check_directories()
    assert c.status == CheckStatus.FAIL
    assert "unwritable runtime directories" in c.detail
    assert "WISP_DATA_DIR" in c.hint


# ─── REPORT RENDERING & COMMAND EXIT CODES ───────────────────────


def test_render_report_formatting() -> None:
    checks = [
        Check("daemon", CheckStatus.OK, "running"),
        Check("membership", CheckStatus.WARN, "not in group", "run usermod"),
        Check("pulumi", CheckStatus.FAIL, "not found", "install pulumi"),
        Check("session-marker", CheckStatus.SKIP, "none"),
    ]
    report = render_report(checks)
    assert report.startswith("Wisp doctor\n\n")
    assert "[ok]   daemon" in report
    assert "[warn] membership" in report
    assert "hint: run usermod" in report
    assert "[fail] pulumi" in report
    assert "hint: install pulumi" in report
    assert "[skip] session-marker" in report
    assert "1 ok, 1 warning, 1 failed, 1 skipped" in report
    assert "Fix the failures above" in report


def test_run_doctor_command_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. FAIL returns 1
    monkeypatch.setattr(
        doctor,
        "run_doctor",
        lambda: [Check("daemon", CheckStatus.FAIL, "socket missing", "install")],
    )
    exit_code = run_doctor_command()
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "1 failed" in captured.out

    # 2. WARN + SKIP only returns 0
    monkeypatch.setattr(
        doctor,
        "run_doctor",
        lambda: [
            Check("membership", CheckStatus.WARN, "warning", "fix it"),
            Check("session-marker", CheckStatus.SKIP, "skipped"),
        ],
    )
    exit_code = run_doctor_command()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "0 failed" in captured.out

    # 3. OK only returns 0
    monkeypatch.setattr(
        doctor,
        "run_doctor",
        lambda: [Check("daemon", CheckStatus.OK, "running")],
    )
    exit_code = run_doctor_command()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "1 ok" in captured.out


# ─── CLI PARSER TESTS ────────────────────────────────────────────


def test_parse_args_doctor() -> None:
    args = parse_args(["doctor"])
    assert args["command"] == CommandEnum.DOCTOR
    assert args["log_level"] == "info"

    args = parse_args(["doctor", "--log-level", "debug"])
    assert args["command"] == CommandEnum.DOCTOR
    assert args["log_level"] == "debug"


# ─── HINT INVARIANT TEST ─────────────────────────────────────────


def test_all_checks_have_hints_on_warn_or_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every check must have a non-empty hint on WARN or FAIL, and empty on OK or SKIP."""
    # Force all checks to fail or warn by setting broken mocks
    monkeypatch.setattr(doctor, "_is_windows", lambda: False)
    monkeypatch.setattr(
        os,
        "stat",
        _intercept_stat("/run/wisp.sock", exc=FileNotFoundError("no socket")),
    )
    orig_which = doctor.shutil.which
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda cmd: (
            None
            if cmd in ("pulumi", "wg", "wg-quick", "systemd-run")
            else orig_which(cmd)
        ),
    )
    orig_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: False if ".pulumi" in str(self) else orig_is_file(self),
    )
    monkeypatch.setattr(doctor, "read_active_session", lambda: None)
    monkeypatch.setattr(doctor, "_is_aws_configured", lambda _: False)
    monkeypatch.setattr(doctor, "_is_oci_configured", lambda _: False)
    monkeypatch.setattr(doctor, "_is_gcp_configured", lambda _: False)

    from wisp.config.settings import WispConfig

    monkeypatch.setattr(
        doctor,
        "load_wisp_config",
        lambda: WispConfig(max_session_ttl_minutes=60),
    )

    fake_grp = types.ModuleType("grp")
    fake_grp.getgrnam = lambda _: (_ for _ in ()).throw(KeyError("wisp"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "grp", fake_grp)

    checks = run_doctor()
    assert len(checks) == 11
    for c in checks:
        if c.status in (CheckStatus.WARN, CheckStatus.FAIL):
            assert c.hint != "", (
                f"Check '{c.name}' with status {c.status} has empty hint"
            )
        elif c.status in (CheckStatus.OK, CheckStatus.SKIP):
            assert c.hint == "", (
                f"Check '{c.name}' with status {c.status} has non-empty hint"
            )
