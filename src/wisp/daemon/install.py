"""Install and uninstall the privileged local daemon.

Provides the ``wisp daemon install`` / ``wisp daemon uninstall`` commands:

- **Linux**: builds a **root-owned venv** at ``/usr/local/lib/wisp/venv``,
  installs ``wisp-vpn`` into it, and validates that the venv's interpreter
  actually imports ``wisp.daemon.server`` *before* touching systemd. The base
  interpreter is picked from a candidate cascade (``python3.14``, then
  ``python3.13``, ``python3.12``, then ``python3``): the first candidate that
  survives *create venv → pip install → import the server* wins, and each
  failed attempt's half-built venv is removed before the next is tried. The
  service unit's ``ExecStart`` points at that venv's interpreter — never at
  the interpreter running Wisp. Reason (live-verified): a root service must
  not execute user-mutable code; pointing ``ExecStart`` at the invoking
  user's ``uv tool`` venv let that venv invalidate itself under the user and
  systemd failed the service with ``203/EXEC``. After validation the units
  are rendered from package data and installed into ``/etc/systemd/system``
  as a socket-activated service, creating the ``wisp`` group and adding the
  invoking sudo user to it. ``uninstall`` removes the units and the
  root-owned venv directory.
- **Windows**: dispatches to the pywin32 service wrapper
  (``wisp.daemon.windows_service``) to install/update/remove the native
  service.
- Any other platform: a clear "not supported" error.

This module is imported lazily by ``wisp.main``: importing anything from the
``wisp.daemon`` package sets the process-local ``WISP_DAEMON=1`` marker, which
would flip the CLI logger to the daemon's stderr-only sink for every command.
"""

import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from wisp.utils import PlatformEnum, logger

#: Target directory for the systemd units (module-level constant so tests can
#: redirect it; override by monkeypatching, not by parameter).
LINUX_UNIT_DIR = Path("/etc/systemd/system")

#: Root-owned installation directory for the daemon runtime; the daemon venv
#: lives at ``LINUX_INSTALL_DIR / "venv"``. The systemd unit executes the
#: interpreter inside this venv, never the invoking user's ``uv tool`` venv:
#: a root service must not depend on user-mutable code (the user-writable
#: uv tool venv invalidated itself under the service and systemd failed with
#: ``203/EXEC``). Module-level constant so tests can redirect it; override by
#: monkeypatching, not by parameter.
LINUX_INSTALL_DIR = Path("/usr/local/lib/wisp")

#: PyPI distribution installed into the daemon venv.
WISP_PACKAGE = "wisp-vpn"

#: Probe that tells whether this machine runs systemd (systemd creates it on
#: every booted system; monkeypatchable in tests).
LINUX_SYSTEMD_DIR = Path("/run/systemd/system")

#: Name of the local group that owns the daemon socket.
WISP_GROUP = "wisp"

#: Packaged unit file names under ``wisp/data``.
SERVICE_TEMPLATE_NAME = "wisp.service.tpl"
SOCKET_UNIT_NAME = "wisp.socket"

_INSTALLED_UNIT_NAMES = ("wisp.socket", "wisp.service")

#: Where the official WireGuard MSI places its tools on Windows.
_WINDOWS_WIREGUARD_DIR = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WireGuard"
)


def _run(
    cmd: list[str], *, ignore_failure: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command, capture its output, and log the outcome.

    Args:
        cmd (list[str]): Command and arguments to execute.
        ignore_failure (bool): When True, a nonzero exit is logged at DEBUG
            and returned instead of being logged as an error (used for
            best-effort stop/disable/reset-failed calls).

    Returns:
        subprocess.CompletedProcess[str]: The completed process (never raises
        on nonzero exit; callers inspect ``returncode``).
    """
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = (
            f"Command failed (rc={result.returncode}): {' '.join(cmd)} — "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        if ignore_failure:
            logger.debug("%s (ignored)", message)
        else:
            logger.error(message)
    return result


def _read_package_data(name: str) -> str:
    """Read a unit template from the package's ``data`` directory.

    Uses the same ``importlib.resources`` mechanism as
    ``wisp.config.constants`` for the WireGuard installer script, so the files
    resolve whether Wisp runs from a wheel install or the source tree.

    Args:
        name (str): File name under ``wisp/data``.

    Returns:
        str: The file's text content.

    Raises:
        FileNotFoundError: If the packaged file is missing.
    """
    resource = files("wisp") / "data" / name
    if not resource.is_file():
        raise FileNotFoundError(f"Packaged unit file not found: wisp/data/{name}")
    return resource.read_text(encoding="utf-8")


def render_service_unit(python_executable: str) -> str:
    """Render the systemd service unit for the given Python interpreter.

    Substitutes the ``{python_executable}`` placeholder in the packaged
    ``wisp.service.tpl`` so the daemon runs under the same interpreter
    that installed it.

    Args:
        python_executable (str): Absolute path to the Python interpreter the
            daemon should run under (normally ``sys.executable``).

    Returns:
        str: The rendered unit file content.

    Raises:
        FileNotFoundError: If the packaged template is missing.
    """
    template = _read_package_data(SERVICE_TEMPLATE_NAME)
    return template.replace("{python_executable}", python_executable)


def load_socket_unit() -> str:
    """Load the systemd socket unit from package data (no variables).

    Returns:
        str: The ``wisp.socket`` unit content, verbatim as packaged.

    Raises:
        FileNotFoundError: If the packaged file is missing.
    """
    return _read_package_data(SOCKET_UNIT_NAME)


def _parse_group_members(getent_output: str) -> set[str]:
    """Parse member usernames from one ``getent group`` output line.

    Args:
        getent_output (str): Raw stdout of ``getent group <name>`` (format
            ``name:password:gid:member1,member2``).

    Returns:
        set[str]: The group's member usernames (empty when there are none).
    """
    entry = getent_output.strip().split(":")
    if len(entry) < 4:
        return set()
    return {member for member in entry[3].split(",") if member}


def _print_stderr(message: str) -> None:
    """Print an error message to stderr (user-facing, not logged)."""
    print(message, file=sys.stderr)


def _systemd_available() -> bool:
    """Return whether this machine runs systemd (best-effort probe).

    Returns:
        bool: True when the systemd runtime directory exists.
    """
    return LINUX_SYSTEMD_DIR.exists()


def _wireguard_missing_linux() -> list[str]:
    """Return the WireGuard tools that are not on PATH.

    Returns:
        list[str]: Missing tool names (empty when everything is installed).
    """
    return [tool for tool in ("wg", "wg-quick") if shutil.which(tool) is None]


def _linux_install_dir() -> Path:
    """Resolve the root-owned daemon runtime directory at call time.

    Indirection keeps ``LINUX_INSTALL_DIR`` monkeypatchable from tests and
    tolerates either a ``Path`` or a string being patched in.

    Returns:
        Path: The install directory (default ``/usr/local/lib/wisp``); the
        daemon venv lives at ``<dir> / "venv"``.
    """
    return Path(LINUX_INSTALL_DIR)


def _venv_base_candidates() -> list[str]:
    """Return the base-interpreter candidates for the daemon venv, in order.

    Probes the well-known interpreter names (newest first) and keeps only the
    ones actually on ``PATH``. There is no hard-coded version floor here: pip
    enforces the package's ``requires-python`` at install time, so the
    cascade self-adapts when the supported range changes.

    Returns:
        list[str]: Interpreter names found on ``PATH``, in preference order
        (``python3.14``, ``python3.13``, ``python3.12``, ``python3``).
    """
    names = ("python3.14", "python3.13", "python3.12", "python3")
    return [name for name in names if shutil.which(name) is not None]


def _discard_daemon_venv(venv_dir: Path) -> None:
    """Remove a half-built daemon venv (best effort, never raises).

    Args:
        venv_dir (Path): Directory of the venv being discarded.
    """
    shutil.rmtree(venv_dir, ignore_errors=True)


def _build_daemon_venv(venv_dir: Path) -> str | None:
    """Build the root-owned daemon venv from the first workable interpreter.

    Iterates the candidates from :func:`_venv_base_candidates` in order; for
    each one it creates the venv, installs ``wisp-vpn`` into it, and
    validates that the venv's interpreter imports ``wisp.daemon.server``.
    The first candidate that survives all three steps wins. A candidate that
    fails at any step has its half-built venv removed (best effort), the
    reason is logged at DEBUG, and the next candidate is tried.

    Args:
        venv_dir (Path): Target directory of the daemon venv.

    Returns:
        str | None: Path of the winning venv interpreter, or ``None`` when no
        candidate produced a working venv (the caller must abort before any
        systemd-related work).
    """
    venv_python = venv_dir / "bin" / "python"
    venv_pip = venv_dir / "bin" / "pip"
    for candidate in _venv_base_candidates():
        logger.debug("Trying base interpreter %s for the daemon venv", candidate)
        created = _run([candidate, "-m", "venv", str(venv_dir)])
        if created.returncode != 0:
            logger.debug(
                "venv creation with %s failed: %s",
                candidate,
                created.stderr.strip() or "unknown error",
            )
            _discard_daemon_venv(venv_dir)
            continue
        installed = _run([str(venv_pip), "install", "--upgrade", "pip", WISP_PACKAGE])
        if installed.returncode != 0:
            logger.debug(
                "pip install into the venv from %s failed: %s",
                candidate,
                installed.stderr.strip() or "unknown error",
            )
            _discard_daemon_venv(venv_dir)
            continue
        # Fail fast: the venv must be able to import the server before
        # anything systemd-related runs and before any unit file is written.
        validated = _run([str(venv_python), "-c", "import wisp.daemon.server"])
        if validated.returncode != 0:
            logger.debug(
                "the venv from %s cannot import the server: %s",
                candidate,
                validated.stderr.strip() or "unknown error",
            )
            _discard_daemon_venv(venv_dir)
            continue
        return str(venv_python)
    return None


def _wireguard_missing_windows() -> list[str]:
    """Return the WireGuard tools missing on Windows.

    Checks the PATH first and falls back to the official MSI install
    directory (``%ProgramFiles%\\WireGuard``), which the service's restricted
    environment may not expose on PATH.

    Returns:
        list[str]: Missing tool names (empty when everything is installed).
    """
    if shutil.which("wg") and shutil.which("wg-quick"):
        return []
    missing: list[str] = []
    for tool in ("wg.exe", "wg-quick.exe"):
        if not (_WINDOWS_WIREGUARD_DIR / tool).is_file():
            missing.append(tool)
    return missing


def _is_windows_admin() -> bool:
    """Return whether the current process runs elevated on Windows.

    Returns:
        bool: True when the process token has administrator rights.
    """
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):  # fmt: skip
        return False


def _wireguard_warning(missing: list[str]) -> None:
    """Print an actionable warning listing missing WireGuard tools."""
    print(f"WARNING: WireGuard tools not found: {', '.join(missing)}")
    print("The daemon needs them at connect time. Install them with:")
    print("  Debian/Ubuntu: sudo apt install wireguard-tools")
    print("  Fedora/RHEL:   sudo dnf install wireguard-tools")
    print("  Arch:          sudo pacman -S wireguard-tools")
    print("  Windows:       https://www.wireguard.com/install/ (official MSI)")


def _detect_platform() -> PlatformEnum:
    """Resolve the current platform.

    Indirection over :meth:`PlatformEnum.get_platform` so callers (and tests)
    can patch the decision point directly instead of patching stdlib globals:
    ``platform.system()`` does not derive from ``sys.platform``, so patching
    the latter cannot influence this module's dispatch.

    Returns:
        PlatformEnum: The detected platform.
    """
    return PlatformEnum.get_platform()


def install_daemon() -> int:
    """Install (or refresh) the privileged local daemon. CLI exit code.

    Returns:
        int: ``0`` on success, nonzero on failure.
    """
    platform = _detect_platform()
    match platform:
        case PlatformEnum.WINDOWS:
            return _install_windows()
        case PlatformEnum.LINUX:
            return _install_linux()
        case _:
            _print_stderr(
                f"Installing the daemon is not supported on this platform "
                f"({sys.platform}). Supported: Linux (systemd), Windows (service)."
            )
    return 1


def uninstall_daemon() -> int:
    """Remove the privileged local daemon. CLI exit code.

    Returns:
        int: ``0`` on success, nonzero on failure.
    """
    platform = _detect_platform()
    match platform:
        case PlatformEnum.WINDOWS:
            return _uninstall_windows()
        case PlatformEnum.LINUX:
            return _uninstall_linux()
        case _:
            _print_stderr(
                f"Uninstalling the daemon is not supported on this platform "
                f"({sys.platform}). Supported: Linux (systemd), Windows (service)."
            )
    return 1


def _install_linux() -> int:
    """Install the systemd units and group on Linux.

    Builds a root-owned venv under :data:`LINUX_INSTALL_DIR`, installs the
    ``wisp-vpn`` package into it, validates the venv imports the daemon
    server, and only then writes the systemd units (whose ``ExecStart``
    points at the venv's interpreter) and configures the group.

    Returns:
        int: ``0`` on success, nonzero on failure.
    """
    if os.geteuid() != 0:
        _print_stderr(
            "wisp daemon install must run as root (try: sudo wisp daemon install)."
        )
        return 1

    if not _systemd_available():
        _print_stderr(
            "systemd was not detected on this system (missing "
            f"{LINUX_SYSTEMD_DIR}). The daemon requires systemd to run as a "
            "socket-activated service; aborting."
        )
        return 1
    missing_tools = _wireguard_missing_linux()
    if missing_tools:
        _wireguard_warning(missing_tools)

    # The daemon must execute root-owned, stable code: build a dedicated venv
    # under LINUX_INSTALL_DIR instead of pointing ExecStart at the invoking
    # user's uv tool venv (user-mutable state that invalidated itself live
    # and failed the service with 203/EXEC). This must succeed before any
    # systemd-related work happens.
    venv_dir = _linux_install_dir() / "venv"
    try:
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _print_stderr(f"Could not create {venv_dir.parent}: {exc}")
        return 1
    venv_python = _build_daemon_venv(venv_dir)
    if venv_python is None:
        _print_stderr(
            "No suitable Python interpreter found to build the daemon venv; "
            "wisp-vpn's requires-python must be satisfiable — install a "
            "newer Python and re-run."
        )
        return 1

    # Ensure the socket's group exists (getent guard: skip when present).
    lookup = _run(["getent", "group", WISP_GROUP])
    members = _parse_group_members(lookup.stdout) if lookup.returncode == 0 else set()
    if lookup.returncode != 0:
        created = _run(["groupadd", WISP_GROUP])
        if created.returncode != 0:
            _print_stderr(
                f"Could not create group '{WISP_GROUP}': "
                f"{created.stderr.strip() or 'unknown error'}"
            )
            return 1
        print(f"Created group '{WISP_GROUP}'.")

    # Render both units from package data, pointing ExecStart at the
    # root-owned venv's interpreter.
    try:
        service_unit = render_service_unit(str(venv_python))
        socket_unit = load_socket_unit()
    except FileNotFoundError as exc:
        _print_stderr(f"Could not read packaged unit templates: {exc}")
        return 1

    # Refresh: stop and disable an existing installation first (best effort).
    unit_dir = _linux_unit_dir()
    if any((unit_dir / name).exists() for name in _INSTALLED_UNIT_NAMES):
        _run(
            ["systemctl", "stop", "wisp.service", "wisp.socket"],
            ignore_failure=True,
        )
        _run(
            ["systemctl", "disable", "wisp.service", "wisp.socket"],
            ignore_failure=True,
        )

    try:
        (unit_dir / SOCKET_UNIT_NAME).write_text(socket_unit, encoding="utf-8")
        (unit_dir / "wisp.service").write_text(service_unit, encoding="utf-8")
    except OSError as exc:
        _print_stderr(f"Could not write unit files to {unit_dir}: {exc}")
        return 1
    print(f"Wrote unit files to {unit_dir}.")

    reload_result = _run(["systemctl", "daemon-reload"])
    if reload_result.returncode != 0:
        _print_stderr("systemctl daemon-reload failed; units may be stale.")
        return 1

    enable_result = _run(["systemctl", "enable", "--now", "wisp.socket"])
    if enable_result.returncode != 0:
        _print_stderr(
            "Could not enable wisp.socket: "
            f"{enable_result.stderr.strip() or 'unknown error'}"
        )
        return 1

    restart_result = _run(["systemctl", "restart", "wisp.service"])
    if restart_result.returncode != 0:
        _print_stderr(
            "Could not start wisp.service: "
            f"{restart_result.stderr.strip() or 'unknown error'}"
        )
        return 1

    # Group membership for the invoking user (the sudoer, not root).
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        if sudo_user not in members:
            usermod = _run(["usermod", "-aG", WISP_GROUP, sudo_user])
            if usermod.returncode != 0:
                _print_stderr(
                    f"Could not add user '{sudo_user}' to group "
                    f"'{WISP_GROUP}': "
                    f"{usermod.stderr.strip() or 'unknown error'}"
                )
                return 1
            print(f"User '{sudo_user}' added to group '{WISP_GROUP}'.")
        else:
            print(f"User '{sudo_user}' is already in group '{WISP_GROUP}'.")
    else:
        print("No SUDO_USER detected. Add your user manually:")
        print(f"  sudo usermod -aG {WISP_GROUP} <user>")

    print(
        "Daemon installed. Close your session and log back in "
        "(or run 'newgrp wisp') for group membership to take effect."
    )
    return 0


def _uninstall_linux() -> int:
    """Remove the systemd units and the root-owned daemon venv on Linux.

    The ``wisp`` group is deliberately kept: its lifecycle (create vs remove)
    belongs to the install/uninstall scripts, which record and consult the
    install manifest.

    Returns:
        int: ``0`` on success, nonzero only when the root check or the unit
        removal fails (a venv-removal failure is reported but non-fatal).
    """
    if os.geteuid() != 0:
        _print_stderr(
            "wisp daemon uninstall must run as root (try: sudo wisp daemon uninstall)."
        )
        return 1

    _run(
        ["systemctl", "disable", "--now", "wisp.service", "wisp.socket"],
        ignore_failure=True,
    )

    unit_dir = _linux_unit_dir()
    removed: list[str] = []
    for name in _INSTALLED_UNIT_NAMES:
        path = unit_dir / name
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as exc:
            _print_stderr(f"Could not remove {path}: {exc}")
            return 1
        removed.append(str(path))

    # Best effort: make systemd forget the removed units and any failure state.
    _run(["systemctl", "daemon-reload"], ignore_failure=True)
    _run(["systemctl", "reset-failed"], ignore_failure=True)

    # Remove the root-owned daemon runtime (its venv) with the units: it was
    # created by `wisp daemon install` and nothing else depends on it. A
    # removal failure is reported but non-fatal (the units are already gone).
    install_dir = _linux_install_dir()
    if install_dir.exists():
        try:
            shutil.rmtree(install_dir)
            removed.append(str(install_dir))
        except OSError as exc:
            _print_stderr(
                f"Could not remove the daemon runtime directory "
                f"{install_dir}: {exc} — remove it manually."
            )

    if removed:
        print("Removed:")
        for path in removed:
            print(f"  - {path}")
    else:
        print("No daemon unit files found; nothing to remove.")
    # The 'wisp' group is deliberately NOT removed: other members or local
    # data may still depend on it, and removing a group is intrusive. Run
    # `sudo groupdel wisp` manually if you are sure nothing needs it.
    print(
        f"Group '{WISP_GROUP}' was kept (remove it manually with "
        "'sudo groupdel wisp' if nothing else needs it)."
    )
    return 0


def _install_windows() -> int:
    """Install the Windows service via the pywin32 wrapper.

    Returns:
        int: ``0`` on success, nonzero on failure (admin rights required).
    """
    if not _is_windows_admin():
        _print_stderr(
            "Installing the Wisp Windows service requires an elevated "
            "(Administrator) terminal. Re-run from an elevated prompt."
        )
        return 1
    missing_tools = _wireguard_missing_windows()
    if missing_tools:
        _wireguard_warning(missing_tools)

    install_result = _run(
        [sys.executable, "-m", "wisp.daemon.windows_service", "install"]
    )
    if install_result.returncode != 0:
        _print_stderr(
            "Could not install the Wisp Windows service. Run the command "
            "from an elevated (Administrator) terminal. Detail: "
            f"{install_result.stderr.strip() or 'unknown error'}"
        )
        return 1
    update_result = _run(
        [
            sys.executable,
            "-m",
            "wisp.daemon.windows_service",
            "--startup",
            "auto",
            "update",
        ]
    )
    if update_result.returncode != 0:
        _print_stderr(
            "Could not set the Wisp service to start automatically. Detail: "
            f"{update_result.stderr.strip() or 'unknown error'}"
        )
        return 1
    print("Wisp daemon service installed (startup: automatic).")
    return 0


def _uninstall_windows() -> int:
    """Remove the Windows service via the pywin32 wrapper.

    Returns:
        int: ``0`` on success, nonzero on failure (admin rights required).
    """
    result = _run([sys.executable, "-m", "wisp.daemon.windows_service", "remove"])
    if result.returncode != 0:
        _print_stderr(
            "Could not remove the Wisp Windows service. Run the command "
            "from an elevated (Administrator) terminal. Detail: "
            f"{result.stderr.strip() or 'unknown error'}"
        )
        return 1
    print("Wisp daemon service removed.")
    return 0


def _linux_unit_dir() -> Path:
    """Resolve the systemd unit directory at call time.

    Indirection keeps ``LINUX_UNIT_DIR`` monkeypatchable from tests and
    tolerates either a ``Path`` or a string being patched in.

    Returns:
        Path: The unit directory (default ``/etc/systemd/system``).
    """
    return Path(LINUX_UNIT_DIR)
