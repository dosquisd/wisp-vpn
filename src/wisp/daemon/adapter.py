import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from wisp.utils import PlatformEnum, logger, secure_file

#: First line written into every client config Wisp installs.
#:
#: Ownership is decided by this marker, never by the path: ``wg0`` is also the
#: default name a user's own WireGuard setup uses, and ``wg-quick`` writes it to
#: the same ``/etc/wireguard/wg0.conf``. Overwriting that file would destroy an
#: unrelated VPN, so the daemon must be able to prove the file is its own.
MANAGED_HEADER = "# Managed by wisp"


class BaseAdapter(ABC):
    WG_CONF_PATH: Path
    __type: PlatformEnum

    @staticmethod
    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a command, raising on non-zero exit, capturing text output."""
        logger.debug(f"[daemon] running: {' '.join(cmd)}")
        return subprocess.run(cmd, check=True, text=True, capture_output=True)

    @abstractmethod
    def connect(self, config_content: str) -> subprocess.CompletedProcess:
        """Connect the local tunnel using the given WireGuard config content."""
        raise NotImplementedError(
            f"{self.__class__.__name__}.connect() must be implemented by subclasses"
        )

    @abstractmethod
    def disconnect(self) -> subprocess.CompletedProcess:
        """Disconnect the local tunnel."""
        raise NotImplementedError(
            f"{self.__class__.__name__}.disconnect() must be implemented by subclasses"
        )

    @abstractmethod
    def status(self) -> subprocess.CompletedProcess:
        """Return the status of the local tunnel."""
        raise NotImplementedError(
            f"{self.__class__.__name__}.status() must be implemented by subclasses"
        )

    def latest_handshake_epoch(self) -> int | None:
        """Return the newest handshake epoch timestamp across all peers, or None.

        Default implementation returns None. Subclasses override this when the
        platform supports querying WireGuard handshake timestamps.

        Returns:
            int | None: The newest handshake epoch, 0 if peers exist but never
            handshook, or None if unknown or unsupported.
        """
        return None

    @property
    def tunnel_name(self) -> str:
        """Return the interface (Linux) or tunnel-service (Windows) name managed."""
        return self.WG_CONF_PATH.stem

    def is_managed(self) -> bool:
        """Return True when the config on disk was written by Wisp.

        The marker is checked instead of the path because other WireGuard
        setups use the same ``wg0`` name and the same directory. A missing or
        unreadable file is not ours by definition.

        Returns:
            bool: True only when the first non-empty line is the ownership
            marker.
        """
        try:
            content = self.WG_CONF_PATH.read_text(errors="replace")
        except OSError:
            return False
        return _has_ownership_header(content)

    def interface_present(self) -> bool:
        """Return True when the tunnel currently exists on this machine.

        Returns:
            bool: True when the platform status query succeeds, False when it
            fails or cannot be run.
        """
        try:
            return self.status().returncode == 0
        except Exception:
            return False

    def foreign_tunnel_reason(self) -> str | None:
        """Explain why the tunnel name cannot be used, or None when it is free.

        Used before any destructive operation. A tunnel is foreign when a
        config with the target name exists without the ownership marker, or
        when the name is live on the machine while the on-disk config is not
        ours - both mean someone else's VPN owns it.

        Returns:
            str | None: A user-facing explanation when the tunnel belongs to
            something else, or None when Wisp may proceed.
        """
        if self.is_managed():
            return None
        if not self.WG_CONF_PATH.exists() and not self.interface_present():
            return None

        return (
            f"WireGuard tunnel '{self.tunnel_name}' already exists and was not "
            f"created by Wisp. Refusing to modify {self.WG_CONF_PATH}, because "
            "overwriting it would destroy another WireGuard configuration. Bring "
            "that tunnel down (or move its config aside if it is a leftover) and "
            "retry."
        )


def _has_ownership_header(content: str) -> bool:
    """Return True when ``content`` starts with Wisp's ownership marker.

    Args:
        content (str): Candidate WireGuard configuration.

    Returns:
        bool: True when the first non-empty line is :data:`MANAGED_HEADER`.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped == MANAGED_HEADER
    return False


def with_ownership_header(config_content: str) -> str:
    """Return ``config_content`` prefixed with the ownership marker.

    ``wg-quick`` ignores ``#`` lines, so the marker is inert for WireGuard and
    is what lets a later run prove the file is Wisp's before overwriting it.

    Args:
        config_content (str): Validated WireGuard client configuration.

    Returns:
        str: The configuration with :data:`MANAGED_HEADER` as its first line.
    """
    return f"{MANAGED_HEADER}\n{config_content}"


def _parse_latest_handshakes(output: str) -> int | None:
    """Parse the output of ``wg show <iface> latest-handshakes``.

    Args:
        output (str): Standard output from the ``latest-handshakes`` command.

    Returns:
        int | None: The max handshake epoch across peers, 0 if peers exist
        but have never handshook, or None if the output is empty or unparseable.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None

    epochs: list[int] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            epochs.append(int(parts[1]))
        except (ValueError, TypeError):
            return None

    return max(epochs) if epochs else None


class LinuxAdapter(BaseAdapter):
    WG_CONF_PATH: Path = Path("/etc/wireguard/wg0.conf")

    __type: PlatformEnum = PlatformEnum.LINUX

    def connect(self, config_content: str) -> subprocess.CompletedProcess:
        self.WG_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.WG_CONF_PATH.write_text(config_content)
        self.WG_CONF_PATH.chmod(0o600)

        # Best-effort: bring down any leftover tunnel of ours before starting
        # fresh. Callers refuse to get here when someone else owns the name, so
        # this can only drop a tunnel Wisp created.
        subprocess.run(
            ["wg-quick", "down", self.tunnel_name], capture_output=True, text=True
        )

        return self._run(["wg-quick", "up", self.tunnel_name])

    def disconnect(self) -> subprocess.CompletedProcess:
        """Disconnect the local tunnel."""
        return self._run(["wg-quick", "down", self.tunnel_name])

    def status(self) -> subprocess.CompletedProcess:
        """Return the status of the local tunnel."""
        return subprocess.run(
            ["wg", "show", self.tunnel_name], capture_output=True, text=True
        )

    def latest_handshake_epoch(self) -> int | None:
        """Return the newest handshake epoch timestamp across all peers.

        Runs ``wg show wg0 latest-handshakes`` and parses the output.

        Returns:
            int | None: The max handshake epoch across peers, 0 if peers exist
            but have never handshook, or None if the interface is down, absent,
            the command fails, or the output is empty or unparseable.
        """
        try:
            res = subprocess.run(
                ["wg", "show", self.WG_CONF_PATH.stem, "latest-handshakes"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                return None
            return _parse_latest_handshakes(res.stdout)
        except Exception:
            return None


class WindowsAdapter(BaseAdapter):
    WG_CONF_PATH: Path = Path("/ProgramData/wisp/wireguard/wg0.conf")

    __type: PlatformEnum = PlatformEnum.WINDOWS

    @property
    def tunnel_service(self) -> str:
        return f"WireGuardTunnel${self.tunnel_name}"

    def connect(self, config_content: str) -> subprocess.CompletedProcess:
        """Connect the local tunnel using the given WireGuard config content.

        Installs the tunnel service via ``wireguard /installtunnelservice`` and
        configures the service start type to manual (``demand``).

        Windows registers the tunnel service with automatic start, so after a
        reboot it would bring a tunnel back up against a server that may already
        be gone, leaving the machine with no working internet until someone tears
        it down manually.

        Args:
            config_content: The full WireGuard client configuration.

        Returns:
            subprocess.CompletedProcess: The result of the install command.

        Raises:
            subprocess.CalledProcessError: If installing the tunnel service fails.
        """
        self.WG_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.WG_CONF_PATH.write_text(config_content, encoding="utf-8")
        secure_file(self.WG_CONF_PATH, platform_enum=PlatformEnum.WINDOWS)

        # Remove any leftover tunnel from a previous session before installing
        # the new one — installtunnelservice fails if the name is already taken.
        subprocess.run(
            ["wireguard", "/uninstalltunnelservice", self.tunnel_name],
            capture_output=True,
            text=True,
        )  # best-effort; ignore failure if it wasn't installed

        proc = self._run(["wireguard", "/installtunnelservice", str(self.WG_CONF_PATH)])

        # WireGuard for Windows registers the tunnel service with automatic start
        # ('auto'). If the system reboots, Windows would bring the tunnel back up
        # against a server that may already be gone, leaving the machine with no
        # working internet until someone tears it down manually. Set start to manual ('demand').
        sc_res = subprocess.run(
            ["sc.exe", "config", self.tunnel_service, "start=", "demand"],
            capture_output=True,
            text=True,
            check=False,
        )
        if sc_res.returncode != 0:
            logger.warning(
                f"Failed to set tunnel service {self.tunnel_service} to manual start: "
                f"{sc_res.stderr.strip() or sc_res.stdout.strip()}"
            )

        return proc

    def disconnect(self) -> subprocess.CompletedProcess:
        return self._run(["wireguard", "/uninstalltunnelservice", self.tunnel_name])

    def status(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sc.exe", "query", self.tunnel_service],
            capture_output=True,
            text=True,
        )

    def latest_handshake_epoch(self) -> int | None:
        """Return the newest handshake epoch timestamp across all peers, or None.

        Runs ``wg show wg0 latest-handshakes`` via subprocess.

        Returns:
            int | None: The newest handshake epoch, 0 if peers exist but never
            handshook, or None on any failure or unparseable output.
        """
        try:
            res = subprocess.run(
                ["wg", "show", self.tunnel_name, "latest-handshakes"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                return None
            return _parse_latest_handshakes(res.stdout)
        except Exception:
            return None


class MacOSAdapter(BaseAdapter):
    WG_CONF_PATH: Path

    __type: PlatformEnum = PlatformEnum.MACOS

    def connect(self, config_content: str) -> subprocess.CompletedProcess:
        raise NotImplementedError("macOS adapter is not implemented yet.")

    def disconnect(self) -> subprocess.CompletedProcess:
        raise NotImplementedError("macOS adapter is not implemented yet.")

    def status(self) -> subprocess.CompletedProcess:
        raise NotImplementedError("macOS adapter is not implemented yet.")


def get_adapter() -> BaseAdapter:
    """Return the appropriate adapter for the current platform."""
    platform_enum = PlatformEnum.get_platform()
    match platform_enum:
        case PlatformEnum.LINUX:
            return LinuxAdapter()
        case PlatformEnum.WINDOWS:
            return WindowsAdapter()
        case PlatformEnum.MACOS:
            return MacOSAdapter()
        case _:
            raise ValueError(f"Unsupported platform: {platform_enum}")
