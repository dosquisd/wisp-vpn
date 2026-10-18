"""Tests for ``wisp daemon install`` / ``wisp daemon uninstall``.

All subprocess calls are mocked at the ``wisp.daemon.install._run`` boundary:
no real ``systemctl``, ``groupadd``, ``usermod``, ``getent``, or ``reg`` runs.
"""

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from wisp.daemon import install as daemon_install
from wisp.daemon.install import (
    install_daemon,
    load_socket_unit,
    render_service_unit,
    uninstall_daemon,
)
from wisp.main import CommandEnum, parse_args
from wisp.utils import PlatformEnum

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    """Replacement for ``daemon_install._run`` that records commands.

    Args:
        results: Mapping from command prefix (tuple of strings) to a
            ``CompletedProcess`` returned when a call matches the prefix.
            Unmatched calls succeed with empty output.
    """

    def __init__(
        self,
        results: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(
        self, cmd: list[str], *, ignore_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        for prefix, result in self.results.items():
            if cmd[: len(prefix)] == list(prefix):
                return result
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def commands(self) -> list[list[str]]:
        """Return the recorded commands (read-only copy)."""
        return [list(call) for call in self.calls]


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Install a default FakeRunner in place of the real ``_run`` helper."""
    fake = FakeRunner()
    monkeypatch.setattr(daemon_install, "_run", fake)
    return fake


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Linux paths believe the process runs as root."""
    monkeypatch.setattr(daemon_install.os, "geteuid", lambda: 0)


# --- Template rendering (package data) --------------------------------------


def test_render_service_unit_substitutes_python_executable() -> None:
    rendered = render_service_unit("/opt/wisp/venv/bin/python")
    assert "ExecStart=/opt/wisp/venv/bin/python -m wisp.daemon.server" in rendered
    assert "User=root" in rendered
    assert "WorkingDirectory" not in rendered
    assert "{python_executable}" not in rendered


def test_socket_unit_matches_packaged_file_verbatim() -> None:
    packaged = REPO_ROOT / "packaging" / "wisp.socket"
    assert load_socket_unit() == packaged.read_text(encoding="utf-8")


def test_unit_templates_resolve_via_importlib_resources() -> None:
    for name in (
        daemon_install.SERVICE_TEMPLATE_NAME,
        daemon_install.SOCKET_UNIT_NAME,
    ):
        resource = files("wisp") / "data" / name
        assert resource.is_file(), f"packaged file missing: wisp/data/{name}"


# --- The _run helper itself (real subprocess, harmless commands) -------------


def test_run_captures_output_and_does_not_raise() -> None:
    ok = daemon_install._run([sys.executable, "-c", "print('out')"])
    assert ok.returncode == 0
    assert "out" in ok.stdout

    failing = daemon_install._run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert failing.returncode == 3


# --- Linux install flow ------------------------------------------------------


def test_install_linux_fresh_creates_group_and_units(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.setenv("SUDO_USER", "testuser")
    # getent fails: the group does not exist yet.
    runner.results = {
        ("getent",): subprocess.CompletedProcess([], 2, stdout="", stderr="")
    }

    exit_code = install_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        ["getent", "group", "wisp"],
        ["groupadd", "wisp"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "wisp.socket"],
        ["usermod", "-aG", "wisp", "testuser"],
    ]
    # No stop/disable: the target dir had no prior units.
    assert not any(call[:2] == ["systemctl", "stop"] for call in runner.commands())
    socket_file = tmp_path / "wisp.socket"
    service_file = tmp_path / "wisp.service"
    assert socket_file.is_file()
    assert service_file.is_file()
    assert socket_file.read_text(encoding="utf-8") == load_socket_unit()
    assert f"ExecStart={sys.executable} -m wisp.daemon.server" in (
        service_file.read_text(encoding="utf-8")
    )
    out = capsys.readouterr().out
    assert "added to group 'wisp'" in out
    assert "log back in" in out


def test_install_linux_refreshes_existing_units(
    runner: FakeRunner, as_root: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    (tmp_path / "wisp.socket").write_text("old", encoding="utf-8")
    (tmp_path / "wisp.service").write_text("old", encoding="utf-8")
    monkeypatch.delenv("SUDO_USER", raising=False)
    # getent succeeds with testuser already a member.
    runner.results = {
        ("getent",): subprocess.CompletedProcess(
            [], 0, stdout="wisp:x:4242:testuser\n", stderr=""
        )
    }

    exit_code = install_daemon()

    assert exit_code == 0
    commands = runner.commands()
    assert commands[0] == ["getent", "group", "wisp"]
    assert ["systemctl", "stop", "wisp.service", "wisp.socket"] in commands
    assert ["systemctl", "disable", "wisp.service", "wisp.socket"] in commands
    assert commands[-2:] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "wisp.socket"],
    ]
    # Group exists and user is already a member: no groupadd, no usermod.
    assert not any(call[0] == "groupadd" for call in commands)
    assert not any(call[0] == "usermod" for call in commands)
    assert (tmp_path / "wisp.service").read_text(encoding="utf-8") != "old"


def test_install_linux_without_sudo_user_prints_manual_instruction(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.delenv("SUDO_USER", raising=False)

    exit_code = install_daemon()

    assert exit_code == 0
    assert not any(call[0] == "usermod" for call in runner.commands())
    captured = capsys.readouterr()
    assert "No SUDO_USER detected" in captured.out
    assert "sudo usermod -aG wisp <user>" in captured.out


def test_install_linux_requires_root(
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install.os, "geteuid", lambda: 1000)

    exit_code = install_daemon()

    assert exit_code == 1
    assert runner.commands() == []
    assert "root" in capsys.readouterr().err


def test_install_linux_fails_when_groupadd_fails(
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner(
        {
            ("getent",): subprocess.CompletedProcess([], 2, stdout="", stderr=""),
            ("groupadd",): subprocess.CompletedProcess(
                [], 1, stdout="", stderr="cannot create group"
            ),
        }
    )
    monkeypatch.setattr(daemon_install, "_run", runner)
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])

    exit_code = install_daemon()

    assert exit_code == 1
    assert "cannot create group" in capsys.readouterr().err
    # Nothing was executed after the failed group creation.
    assert runner.commands() == [["getent", "group", "wisp"], ["groupadd", "wisp"]]


def test_install_linux_requires_systemd(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: False)

    exit_code = install_daemon()

    assert exit_code == 1
    # Nothing at all was executed: systemd is a hard prerequisite.
    assert runner.commands() == []
    assert "systemd" in capsys.readouterr().err


def test_install_linux_warns_when_wireguard_missing(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(
        daemon_install, "_wireguard_missing_linux", lambda: ["wg", "wg-quick"]
    )

    exit_code = install_daemon()

    # Missing tools warn but do not block: WireGuard can be installed later.
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "WARNING: WireGuard tools not found: wg, wg-quick" in captured.out
    assert "wireguard-tools" in captured.out


def test_install_windows_requires_admin(
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        daemon_install, "_detect_platform", lambda: PlatformEnum.WINDOWS
    )
    monkeypatch.setattr(daemon_install, "_is_windows_admin", lambda: False)

    exit_code = install_daemon()

    assert exit_code == 1
    # Nothing dispatched to pywin32: elevation is checked up front.
    assert runner.commands() == []
    assert "elevated (Administrator)" in capsys.readouterr().err


def test_install_windows_warns_when_wireguard_missing(
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        daemon_install, "_detect_platform", lambda: PlatformEnum.WINDOWS
    )
    monkeypatch.setattr(daemon_install, "_is_windows_admin", lambda: True)
    monkeypatch.setattr(
        daemon_install, "_wireguard_missing_windows", lambda: ["wg-quick.exe"]
    )

    exit_code = install_daemon()

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "WARNING: WireGuard tools not found: wg-quick.exe" in captured.out
    assert "wireguard.com/install" in captured.out


# --- Linux uninstall flow ----------------------------------------------------


def test_uninstall_linux_removes_units_and_keeps_group(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    (tmp_path / "wisp.socket").write_text("unit", encoding="utf-8")
    (tmp_path / "wisp.service").write_text("unit", encoding="utf-8")

    exit_code = uninstall_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        ["systemctl", "disable", "--now", "wisp.service", "wisp.socket"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "reset-failed"],
    ]
    assert not (tmp_path / "wisp.socket").exists()
    assert not (tmp_path / "wisp.service").exists()
    # The wisp group is deliberately kept.
    assert not any(call[0] == "groupdel" for call in runner.commands())
    assert "kept" in capsys.readouterr().out


def test_uninstall_linux_without_units_is_a_clean_noop(
    runner: FakeRunner, as_root: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)

    exit_code = uninstall_daemon()

    assert exit_code == 0


def test_uninstall_linux_requires_root(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_install.os, "geteuid", lambda: 1000)

    exit_code = uninstall_daemon()

    assert exit_code == 1
    assert runner.commands() == []


# --- Windows branch (pywin32 service) ----------------------------------------


def test_install_windows_runs_pywin32_service_commands(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_install, "_detect_platform", lambda: PlatformEnum.WINDOWS
    )
    monkeypatch.setattr(daemon_install, "_is_windows_admin", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_windows", lambda: [])

    exit_code = install_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        [sys.executable, "-m", "wisp.daemon.windows_service", "install"],
        [
            sys.executable,
            "-m",
            "wisp.daemon.windows_service",
            "--startup",
            "auto",
            "update",
        ],
    ]


def test_install_windows_surfaces_admin_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner(
        {
            (
                sys.executable,
                "-m",
                "wisp.daemon.windows_service",
                "install",
            ): subprocess.CompletedProcess([], 1, stdout="", stderr="Access is denied"),
        }
    )
    monkeypatch.setattr(daemon_install, "_run", runner)
    monkeypatch.setattr(
        daemon_install, "_detect_platform", lambda: PlatformEnum.WINDOWS
    )
    monkeypatch.setattr(daemon_install, "_is_windows_admin", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_windows", lambda: [])

    exit_code = install_daemon()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Administrator" in captured.err
    assert "Access is denied" in captured.err
    # The update step never ran after the failed install.
    assert len(runner.commands()) == 1


def test_uninstall_windows_runs_pywin32_remove(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_install, "_detect_platform", lambda: PlatformEnum.WINDOWS
    )

    exit_code = uninstall_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        [sys.executable, "-m", "wisp.daemon.windows_service", "remove"]
    ]


# --- Unsupported platform -----------------------------------------------------


@pytest.mark.parametrize("action", [install_daemon, uninstall_daemon])
def test_unsupported_platform_fails_cleanly(
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action,
) -> None:
    monkeypatch.setattr(daemon_install, "_detect_platform", lambda: PlatformEnum.MACOS)

    exit_code = action()

    assert exit_code == 1
    assert runner.commands() == []
    assert "not supported on this platform" in capsys.readouterr().err


# --- CLI wiring ---------------------------------------------------------------


def test_parse_args_daemon_install() -> None:
    args = parse_args(["daemon", "install"])
    assert args["command"] == CommandEnum.DAEMON
    assert args["daemon_command"] == "install"


def test_parse_args_daemon_uninstall() -> None:
    args = parse_args(["daemon", "uninstall"])
    assert args["command"] == CommandEnum.DAEMON
    assert args["daemon_command"] == "uninstall"


def test_parse_args_daemon_requires_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        parse_args(["daemon"])
    assert "required" in capsys.readouterr().err
