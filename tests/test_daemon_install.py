"""Tests for ``wisp daemon install`` / ``wisp daemon uninstall``.

All subprocess calls are mocked at the ``wisp.daemon.install._run`` boundary:
no real ``systemctl``, ``groupadd``, ``usermod``, ``getent``, or ``reg`` runs.
"""

import json
import subprocess
import sys
import types
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

import pytest

from wisp.daemon import install as daemon_install
from wisp.daemon.install import (
    install_daemon,
    load_socket_unit,
    render_service_unit,
    uninstall_daemon,
)
from wisp.main import CommandEnum, parse_args
from wisp.utils import PlatformEnum, privileges

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The unpatchable resolver, captured before the autouse fixture below replaces
#: it, so the resolution tests can exercise the real implementation.
RESOLVE_INSTALL_TARGET = daemon_install._daemon_install_target


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


class VenvSimulatingRunner(FakeRunner):
    """FakeRunner that also simulates successful venv creation.

    When an unmatched ``<interpreter> -m venv <dir>`` call succeeds, the
    target directory is actually created, so tests can assert cleanup of
    half-built venvs.
    """

    def __call__(
        self, cmd: list[str], *, ignore_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        for prefix, result in self.results.items():
            if cmd[: len(prefix)] == list(prefix):
                return result
        if len(cmd) > 3 and cmd[1:3] == ["-m", "venv"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Linux paths believe the process runs as root."""
    monkeypatch.setattr(daemon_install.os, "geteuid", lambda: 0)


@pytest.fixture(autouse=True)
def pinned_install_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the daemon venv to the package name for the install-flow tests.

    The real resolver mirrors a local checkout, and ``uv run`` installs wisp
    exactly that way, so without this every command assertion would depend on
    how the test run itself installed the project. The resolver has its own
    tests below, which bypass this fixture.
    """
    monkeypatch.setattr(
        daemon_install,
        "_daemon_install_target",
        lambda: daemon_install.WISP_PACKAGE,
    )


@pytest.fixture
def linux_env(
    runner: FakeRunner, as_root: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Patch the Linux install preconditions; return the unit directory.

    Redirects the unit dir and the daemon runtime dir into ``tmp_path``,
    fakes systemd availability, no missing WireGuard tools, and a single
    fake base interpreter for the daemon venv.
    """
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", unit_dir)
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.setattr(
        daemon_install, "_venv_base_candidates", lambda: ["fake-python"]
    )
    return unit_dir


def venv_paths(install_dir: Path) -> tuple[Path, Path, Path]:
    """Return ``(venv_dir, venv_python, venv_pip)`` for an install dir."""
    venv_dir = install_dir / "venv"
    return venv_dir, venv_dir / "bin" / "python", venv_dir / "bin" / "pip"


def units_written(unit_dir: Path) -> bool:
    """Return whether both systemd unit files exist in ``unit_dir``."""
    return (unit_dir / "wisp.socket").is_file() and (
        unit_dir / "wisp.service"
    ).is_file()


# --- Daemon venv install target (PyPI release vs local checkout) -------------


def _write_direct_url(path: Path, url: str, *, editable: bool = True) -> Path:
    """Write a PEP 610 ``direct_url.json`` and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"url": url}
    if editable:
        document["dir_info"] = {"editable": True}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_local_source_from_direct_url_reads_editable_checkout(
    tmp_path: Path,
) -> None:
    """An editable install records the checkout it came from."""
    source = tmp_path / "wisp"
    source.mkdir()
    direct_url = _write_direct_url(
        tmp_path / "dist-info" / "direct_url.json", f"file://{source}"
    )

    assert daemon_install._local_source_from_direct_url(direct_url) == source


def test_local_source_from_direct_url_decodes_percent_escapes(
    tmp_path: Path,
) -> None:
    """PEP 610 URLs are percent-encoded; the path must be decoded."""
    source = tmp_path / "my project"
    source.mkdir()
    direct_url = _write_direct_url(
        tmp_path / "dist-info" / "direct_url.json",
        f"file://{quote(str(source))}",
    )

    assert daemon_install._local_source_from_direct_url(direct_url) == source


def test_local_source_from_direct_url_rejects_non_file_urls(tmp_path: Path) -> None:
    """A wheel or an index URL is not a checkout."""
    direct_url = _write_direct_url(
        tmp_path / "direct_url.json",
        "https://files.pythonhosted.org/packages/wisp_vpn-0.1.3-py3-none-any.whl",
        editable=False,
    )

    assert daemon_install._local_source_from_direct_url(direct_url) is None


def test_local_source_from_direct_url_rejects_missing_directory(
    tmp_path: Path,
) -> None:
    """A checkout that was moved or deleted must not be installed from."""
    direct_url = _write_direct_url(
        tmp_path / "direct_url.json", f"file://{tmp_path / 'gone'}"
    )

    assert daemon_install._local_source_from_direct_url(direct_url) is None


def test_local_source_from_direct_url_rejects_malformed_file(tmp_path: Path) -> None:
    """A half-written or corrupt metadata file degrades to "no source"."""
    direct_url = tmp_path / "direct_url.json"
    direct_url.write_text("{not json", encoding="utf-8")

    assert daemon_install._local_source_from_direct_url(direct_url) is None
    assert daemon_install._local_source_from_direct_url(tmp_path / "absent") is None


def test_daemon_install_target_mirrors_local_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local CLI makes the daemon install the same checkout."""
    source = tmp_path / "wisp"
    source.mkdir()
    direct_url = _write_direct_url(tmp_path / "direct_url.json", f"file://{source}")

    class _Distribution:
        files = [types.SimpleNamespace(name="direct_url.json")]

        @staticmethod
        def locate_file(entry: object) -> Path:
            return direct_url

    monkeypatch.setattr(
        daemon_install.metadata, "distribution", lambda name: _Distribution()
    )

    assert RESOLVE_INSTALL_TARGET() == str(source)


def test_daemon_install_target_uses_release_without_direct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index install has no ``direct_url.json``; keep the published name."""

    class _Distribution:
        files = [types.SimpleNamespace(name="METADATA")]

        @staticmethod
        def locate_file(entry: object) -> Path:  # pragma: no cover - never used
            raise AssertionError("no direct_url.json should be read")

    monkeypatch.setattr(
        daemon_install.metadata, "distribution", lambda name: _Distribution()
    )

    assert RESOLVE_INSTALL_TARGET() == daemon_install.WISP_PACKAGE


def test_daemon_install_target_uses_release_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing distribution must not break the install."""

    def _missing(name: str) -> None:
        raise daemon_install.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(daemon_install.metadata, "distribution", _missing)

    assert RESOLVE_INSTALL_TARGET() == daemon_install.WISP_PACKAGE


def test_staged_install_target_copies_checkout_without_vcs_or_build_dirs(
    tmp_path: Path,
) -> None:
    """Staging copies the sources and skips what the build cannot need."""
    source = tmp_path / "wisp"
    (source / "src" / "wisp").mkdir(parents=True)
    (source / "src" / "wisp" / "__init__.py").write_text("", encoding="utf-8")
    (source / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / "build").mkdir()

    with daemon_install._staged_install_target(str(source)) as staged:
        staged_path = Path(staged)
        assert staged_path != source
        assert (staged_path / "pyproject.toml").is_file()
        assert (staged_path / "src" / "wisp" / "__init__.py").is_file()
        assert not (staged_path / ".git").exists()
        assert not (staged_path / ".venv").exists()
        assert not (staged_path / "build").exists()

    assert not staged_path.exists()
    assert (source / "pyproject.toml").is_file()


def test_staged_install_target_passes_package_name_through() -> None:
    """A PyPI target is handed to pip untouched, with no staging at all."""
    with daemon_install._staged_install_target(daemon_install.WISP_PACKAGE) as target:
        assert target == daemon_install.WISP_PACKAGE


def test_install_linux_builds_daemon_venv_from_local_checkout(
    linux_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``wisp daemon install`` builds from a staged copy, never from the repo.

    Building in place under ``sudo`` would drop root-owned ``build/`` and
    ``*.egg-info`` into the developer's checkout.
    """
    source = tmp_path / "checkout"
    (source / "src" / "wisp").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    monkeypatch.setattr(daemon_install, "_daemon_install_target", lambda: str(source))

    seen_targets: list[str] = []

    def fake_run(
        cmd: list[str], *, ignore_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if cmd[1:3] == ["install", "--upgrade"]:
            seen_targets.append(cmd[-1])
            # The staged copy must exist while pip runs.
            assert Path(cmd[-1]).is_dir()
        if len(cmd) > 3 and cmd[1:3] == ["-m", "venv"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(daemon_install, "_run", fake_run)
    monkeypatch.delenv("SUDO_USER", raising=False)

    assert install_daemon() == 0
    assert len(seen_targets) == 1
    staged = Path(seen_targets[0])
    assert staged != source
    assert staged.name == source.name
    assert not staged.exists()
    assert not (source / "build").exists()
    assert not list(source.glob("*.egg-info"))


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
    linux_env: Path,
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUDO_USER", "testuser")
    # getent fails: the group does not exist yet.
    runner.results = {
        ("getent",): subprocess.CompletedProcess([], 2, stdout="", stderr="")
    }
    venv_dir, venv_python, venv_pip = venv_paths(tmp_path / "opt")

    exit_code = install_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        ["fake-python", "-m", "venv", str(venv_dir)],
        [str(venv_pip), "install", "--upgrade", "pip", "wisp-vpn"],
        [str(venv_python), "-c", "import wisp.daemon.server"],
        ["getent", "group", "wisp"],
        ["groupadd", "wisp"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "wisp.socket"],
        ["systemctl", "restart", "wisp.service"],
        ["usermod", "-aG", "wisp", "testuser"],
    ]
    # No stop/disable: the target dir had no prior units.
    assert not any(call[:2] == ["systemctl", "stop"] for call in runner.commands())
    socket_file = linux_env / "wisp.socket"
    service_file = linux_env / "wisp.service"
    assert socket_file.is_file()
    assert service_file.is_file()
    assert socket_file.read_text(encoding="utf-8") == load_socket_unit()
    assert f"ExecStart={venv_python} -m wisp.daemon.server" in (
        service_file.read_text(encoding="utf-8")
    )
    out = capsys.readouterr().out
    assert "added to group 'wisp'" in out
    assert "log back in" in out


def test_install_linux_refreshes_existing_units(
    linux_env: Path,
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (linux_env / "wisp.socket").write_text("old", encoding="utf-8")
    (linux_env / "wisp.service").write_text("old", encoding="utf-8")
    monkeypatch.delenv("SUDO_USER", raising=False)
    # getent succeeds with testuser already a member.
    runner.results = {
        ("getent",): subprocess.CompletedProcess(
            [], 0, stdout="wisp:x:4242:testuser\n", stderr=""
        )
    }
    venv_dir, venv_python, venv_pip = venv_paths(tmp_path / "opt")

    exit_code = install_daemon()

    assert exit_code == 0
    commands = runner.commands()
    assert commands[:4] == [
        ["fake-python", "-m", "venv", str(venv_dir)],
        [str(venv_pip), "install", "--upgrade", "pip", "wisp-vpn"],
        [str(venv_python), "-c", "import wisp.daemon.server"],
        ["getent", "group", "wisp"],
    ]
    assert ["systemctl", "stop", "wisp.service", "wisp.socket"] in commands
    assert ["systemctl", "disable", "wisp.service", "wisp.socket"] in commands
    assert commands[-3:] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "wisp.socket"],
        ["systemctl", "restart", "wisp.service"],
    ]
    # Group exists and user is already a member: no groupadd, no usermod.
    assert not any(call[0] == "groupadd" for call in commands)
    assert not any(call[0] == "usermod" for call in commands)
    assert (linux_env / "wisp.service").read_text(encoding="utf-8") != "old"


def test_install_linux_without_sudo_user_prints_manual_instruction(
    linux_env: Path,
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    monkeypatch.setattr(
        privileges.shutil, "which", lambda name: "/home/u/.local/bin/wisp"
    )

    exit_code = install_daemon()

    assert exit_code == 1
    assert runner.commands() == []
    stderr = capsys.readouterr().err
    assert "root" in stderr
    assert 'sudo env PYTHONDONTWRITEBYTECODE=1 "/home/u/.local/bin/wisp"' in stderr


def test_install_linux_fails_when_groupadd_fails(
    linux_env: Path,
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner.results = {
        ("getent",): subprocess.CompletedProcess([], 2, stdout="", stderr=""),
        ("groupadd",): subprocess.CompletedProcess(
            [], 1, stdout="", stderr="cannot create group"
        ),
    }
    venv_dir, venv_python, venv_pip = venv_paths(tmp_path / "opt")

    exit_code = install_daemon()

    assert exit_code == 1
    assert "cannot create group" in capsys.readouterr().err
    # Nothing was executed after the failed group creation.
    assert runner.commands() == [
        ["fake-python", "-m", "venv", str(venv_dir)],
        [str(venv_pip), "install", "--upgrade", "pip", "wisp-vpn"],
        [str(venv_python), "-c", "import wisp.daemon.server"],
        ["getent", "group", "wisp"],
        ["groupadd", "wisp"],
    ]


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
    linux_env: Path,
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_install_linux_fails_when_service_restart_fails(
    linux_env: Path,
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    runner.results = {
        ("systemctl", "restart"): subprocess.CompletedProcess(
            [], 1, stdout="", stderr="unit is masked"
        )
    }

    exit_code = install_daemon()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Could not start wisp.service" in captured.err
    assert "unit is masked" in captured.err
    # No group-membership work happens after a failed service start.
    assert not any(call[0] == "usermod" for call in runner.commands())


# --- Daemon venv candidate cascade -------------------------------------------


def test_install_linux_all_candidates_fail_aborts_before_systemd(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_dir = tmp_path / "units"
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", unit_dir)
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.setattr(
        daemon_install, "_venv_base_candidates", lambda: ["fake-a", "fake-b"]
    )
    runner.results = {
        ("fake-a",): subprocess.CompletedProcess([], 1, stdout="", stderr="boom-a"),
        ("fake-b",): subprocess.CompletedProcess([], 1, stdout="", stderr="boom-b"),
    }

    exit_code = install_daemon()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No suitable Python interpreter found" in captured.err
    assert "install a newer Python and re-run" in captured.err
    # Fail-fast guarantee: no systemd command ran and no unit file was written.
    assert not any(call[0] == "systemctl" for call in runner.commands())
    assert not any(call[0] == "getent" for call in runner.commands())
    assert list(unit_dir.glob("*")) == []


def test_install_linux_second_candidate_succeeds(
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = VenvSimulatingRunner(
        {
            # The first candidate fails at venv creation; the second works.
            ("fake-a",): subprocess.CompletedProcess([], 1, stdout="", stderr="boom-a"),
        }
    )
    monkeypatch.setattr(daemon_install, "_run", runner)
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.setattr(
        daemon_install, "_venv_base_candidates", lambda: ["fake-a", "fake-b"]
    )
    venv_dir, venv_python, venv_pip = venv_paths(tmp_path / "opt")

    exit_code = install_daemon()

    assert exit_code == 0
    commands = runner.commands()
    # Both candidates attempted venv creation, in order; only the winner's
    # venv survives on disk.
    assert commands.count(["fake-a", "-m", "venv", str(venv_dir)]) == 1
    assert commands.count(["fake-b", "-m", "venv", str(venv_dir)]) == 1
    assert commands.index(["fake-a", "-m", "venv", str(venv_dir)]) < commands.index(
        ["fake-b", "-m", "venv", str(venv_dir)]
    )
    # pip install + validation ran once, for the winning candidate.
    pip_cmd = [str(venv_pip), "install", "--upgrade", "pip", "wisp-vpn"]
    assert commands.count(pip_cmd) == 1
    assert commands.count([str(venv_python), "-c", "import wisp.daemon.server"]) == 1
    assert venv_dir.is_dir()
    assert units_written(tmp_path / "units")


@pytest.mark.parametrize(
    ("fail_stage", "failing_prefix"),
    [
        ("creation", "venv"),
        ("pip-install", "pip"),
        ("validation", "python"),
    ],
)
def test_install_linux_candidate_fails_cleaned_up(
    fail_stage: str,
    failing_prefix: str,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = VenvSimulatingRunner()
    monkeypatch.setattr(daemon_install, "_run", runner)
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.setattr(
        daemon_install, "_venv_base_candidates", lambda: ["fake-python"]
    )
    venv_dir, venv_python, venv_pip = venv_paths(tmp_path / "opt")
    match failing_prefix:
        case "venv":
            runner.results = {
                ("fake-python", "-m", "venv"): subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="venv boom"
                )
            }
        case "pip":
            runner.results = {
                (str(venv_pip),): subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="pip boom"
                )
            }
        case "python":
            runner.results = {
                (str(venv_python), "-c"): subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="ModuleNotFoundError"
                )
            }

    exit_code = install_daemon()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "No suitable Python interpreter found" in captured.err
    # The half-built venv was discarded before giving up.
    assert not venv_dir.exists()
    assert not any(call[0] == "systemctl" for call in runner.commands())
    assert list((tmp_path / "units").glob("*")) == []


def test_install_linux_validation_failure_on_last_candidate_no_units(
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = VenvSimulatingRunner()
    monkeypatch.setattr(daemon_install, "_run", runner)
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    monkeypatch.setattr(daemon_install, "_systemd_available", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_linux", lambda: [])
    monkeypatch.setattr(
        daemon_install, "_venv_base_candidates", lambda: ["fake-python"]
    )
    venv_dir, venv_python, _ = venv_paths(tmp_path / "opt")
    runner.results = {
        (str(venv_python), "-c"): subprocess.CompletedProcess(
            [], 1, stdout="", stderr="ModuleNotFoundError"
        ),
    }

    exit_code = install_daemon()

    assert exit_code == 1
    assert "No suitable Python interpreter found" in capsys.readouterr().err
    # Fail-fast guarantee on the LAST candidate: no systemctl commands and
    # no unit files anywhere.
    assert not any(call[0] == "systemctl" for call in runner.commands())
    assert list((tmp_path / "units").glob("*")) == []
    assert not venv_dir.exists()


# --- Linux uninstall flow ----------------------------------------------------


def test_uninstall_linux_removes_units_runtime_dir_and_keeps_group(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    (tmp_path / "units" / "wisp.socket").write_text("unit", encoding="utf-8")
    (tmp_path / "units" / "wisp.service").write_text("unit", encoding="utf-8")
    venv_bin = tmp_path / "opt" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("stub", encoding="utf-8")

    exit_code = uninstall_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        ["systemctl", "disable", "--now", "wisp.service", "wisp.socket"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "reset-failed"],
    ]
    assert not (tmp_path / "units" / "wisp.socket").exists()
    assert not (tmp_path / "units" / "wisp.service").exists()
    # The root-owned daemon runtime is removed together with the units.
    assert not (tmp_path / "opt").exists()
    out = capsys.readouterr().out
    assert str(tmp_path / "opt") in out
    # The wisp group is deliberately kept.
    assert "was kept" in out
    assert not any(call[0] == "groupdel" for call in runner.commands())


def test_uninstall_linux_reports_runtime_dir_removal_failure(
    runner: FakeRunner,
    as_root: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")
    (tmp_path / "units" / "wisp.socket").write_text("unit", encoding="utf-8")
    (tmp_path / "units" / "wisp.service").write_text("unit", encoding="utf-8")
    (tmp_path / "opt").mkdir()

    def failing_rmtree(path, *args, **kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(daemon_install.shutil, "rmtree", failing_rmtree)

    exit_code = uninstall_daemon()

    # Non-fatal: the units are already gone, only the runtime dir removal
    # failed and is reported.
    assert exit_code == 0
    assert (tmp_path / "units" / "wisp.socket").exists() is False
    assert (tmp_path / "units" / "wisp.service").exists() is False
    assert (tmp_path / "opt").is_dir()
    captured = capsys.readouterr()
    assert "Could not remove the daemon runtime directory" in captured.err
    assert "device busy" in captured.err


def test_uninstall_linux_without_units_is_a_clean_noop(
    runner: FakeRunner, as_root: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon_install, "LINUX_UNIT_DIR", tmp_path)
    monkeypatch.setattr(daemon_install, "LINUX_INSTALL_DIR", tmp_path / "opt")

    exit_code = uninstall_daemon()

    assert exit_code == 0


def test_uninstall_linux_requires_root(
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(daemon_install.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        privileges.shutil, "which", lambda name: "/home/u/.local/bin/wisp"
    )

    exit_code = uninstall_daemon()

    assert exit_code == 1
    assert runner.commands() == []
    assert "root" in capsys.readouterr().err


# --- Windows branch (pywin32 service) ----------------------------------------


def test_install_windows_runs_pywin32_service_commands(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon_install, "_detect_platform", lambda: PlatformEnum.WINDOWS
    )
    monkeypatch.setattr(daemon_install, "_is_windows_admin", lambda: True)
    monkeypatch.setattr(daemon_install, "_wireguard_missing_windows", lambda: [])
    monkeypatch.setattr(daemon_install, "_ensure_windows_group", lambda: True)
    monkeypatch.setattr(
        daemon_install, "_prepare_windows_service_runtime", lambda: True
    )

    exit_code = install_daemon()

    assert exit_code == 0
    assert runner.commands() == [
        [
            sys.executable,
            "-m",
            "wisp.daemon.windows_service",
            "remove",
        ],
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


def test_ensure_windows_group_accepts_existing_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(
        cmd: list[str], *, ignore_failure: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if cmd == ["whoami"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="DESKTOP\\juand\n", stderr=""
            )
        if cmd == ["net", "localgroup", "wisp"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Alias name     wisp\nMembers\n", stderr=""
            )
        if cmd == ["net", "localgroup", "wisp", "DESKTOP\\juand", "/add"]:
            return subprocess.CompletedProcess(
                cmd, 2, stdout="", stderr="Error de sistema 1378.\n"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(daemon_install, "_run", run)

    assert daemon_install._ensure_windows_group() is True


def test_prepare_windows_service_runtime_skips_identical_locked_dlls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_dir = tmp_path / "venv"
    dll_dir = venv_dir / "Lib" / "site-packages" / "pywin32_system32"
    dll_dir.mkdir(parents=True)
    source = dll_dir / "pywintypes.dll"
    destination = venv_dir / source.name
    source.write_bytes(b"runtime")
    destination.write_bytes(b"runtime")
    monkeypatch.setattr(
        daemon_install.sys,
        "executable",
        str(venv_dir / "Scripts" / "python.exe"),
    )

    assert daemon_install._prepare_windows_service_runtime() is True


def test_prepare_windows_service_runtime_copies_python_runtime_dll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_dir = tmp_path / "venv"
    dll_dir = venv_dir / "Lib" / "site-packages" / "pywin32_system32"
    dll_dir.mkdir(parents=True)
    base_prefix = tmp_path / "python"
    base_prefix.mkdir()
    (base_prefix / "python314.dll").write_bytes(b"python-runtime")
    monkeypatch.setattr(
        daemon_install.sys,
        "executable",
        str(venv_dir / "Scripts" / "python.exe"),
    )
    monkeypatch.setattr(daemon_install.sys, "base_prefix", str(base_prefix))

    assert daemon_install._prepare_windows_service_runtime() is True
    assert (venv_dir / "python314.dll").read_bytes() == b"python-runtime"


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
    monkeypatch.setattr(daemon_install, "_ensure_windows_group", lambda: True)
    monkeypatch.setattr(
        daemon_install, "_prepare_windows_service_runtime", lambda: True
    )

    exit_code = install_daemon()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Administrator" in captured.err
    assert "Access is denied" in captured.err
    # The update step never ran after the failed install.
    assert len(runner.commands()) == 2


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
