"""Remote WireGuard server configuration via Paramiko (SSH/SFTP)."""

import re
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path

import paramiko

from wisp.config.constants import (
    WATCHDOG_SCRIPT_PATH,
    WIREGUARD_CLIENT_CONF_PATH,
    WIREGUARD_SCRIPT_PATH,
)
from wisp.schemas import InventoryContext
from wisp.utils import logger

#: Path of the provenance manifest written by a successful server install.
INSTALLED_PACKAGES_PATH = "/etc/wisp/installed-packages"

#: Packages a destroy may remove, and only when Wisp installed them.
#:
#: ``iptables`` and ``resolvconf`` are absent on purpose: the installer adds
#: them on most distros, but a server may rely on them for its own firewall or
#: DNS. "Wisp installed it once" is not evidence that nobody uses it now, and
#: taking packet filtering away from a machine Wisp does not own is never a
#: safe cleanup. ``libqrencode-tools`` is Alpine's name for ``qrencode``.
REMOVABLE_PACKAGES = frozenset(
    {"wireguard", "wireguard-tools", "qrencode", "libqrencode-tools"}
)

#: Package names per family for each tool the installer provides as a command.
_PACKAGES_BY_FAMILY: dict[str, dict[str, tuple[str, ...]]] = {
    "apt": {"wg": ("wireguard", "wireguard-tools"), "qrencode": ("qrencode",)},
    "dnf": {"wg": ("wireguard-tools",), "qrencode": ("qrencode",)},
    "yum": {"wg": ("wireguard-tools",), "qrencode": ("qrencode",)},
    "pacman": {"wg": ("wireguard-tools",), "qrencode": ("qrencode",)},
    "apk": {"wg": ("wireguard-tools",), "qrencode": ("libqrencode-tools",)},
}

#: Removal command per package family (mirrors ``scripts/uninstall.sh``).
_REMOVAL_COMMANDS: dict[str, str] = {
    "apt": "apt-get remove -y",
    "dnf": "dnf remove -y",
    "yum": "yum remove -y",
    "pacman": "pacman -Rs --noconfirm",
    "apk": "apk del",
}


def _privileged_prefix(ssh_user: str) -> str:
    """Return the shell prefix needed to run a command as root.

    Args:
        ssh_user (str): Remote username.

    Returns:
        str: ``""`` when connected as root, otherwise ``"sudo -n "``.
    """
    return "" if ssh_user == "root" else "sudo -n "


def _detect_package_family(ssh: paramiko.SSHClient, ssh_user: str) -> str:
    """Detect which package manager can remove packages on the server.

    Detection asks the server which managers exist instead of parsing
    ``/etc/os-release``: the removal path only needs a command that works, and
    containers or minimal images do not always match their distro defaults.

    Args:
        ssh (paramiko.SSHClient): Connected SSH client.
        ssh_user (str): Remote username (root skips ``sudo``).

    Returns:
        str: ``"apt"``, ``"dnf"``, ``"yum"``, ``"pacman"``, ``"apk"``, or an
        empty string when none was found.
    """
    prefix = _privileged_prefix(ssh_user)
    status, stdout, _ = _run_remote(
        ssh,
        f"{prefix}sh -c 'for m in apt-get dnf yum pacman apk; do"
        " command -v $m >/dev/null 2>&1 && { echo $m; break; }; done'",
    )
    if status != 0:
        return ""
    manager = stdout.strip()
    return "apt" if manager == "apt-get" else manager


def _missing_tool_commands(ssh: paramiko.SSHClient, ssh_user: str) -> list[str]:
    """List the installer tool commands the server does not have yet.

    The installer provides WireGuard through ``wg`` and prints client QR codes
    through ``qrencode``. Probing the commands instead of the package names
    covers the installer's per-distro choices (``wireguard-tools`` vs
    ``wireguard``, ``qrencode`` vs ``libqrencode-tools``).

    Args:
        ssh (paramiko.SSHClient): Connected SSH client.
        ssh_user (str): Remote username (root skips ``sudo``).

    Returns:
        list[str]: Command names that are missing, or an empty list when the
        probe itself failed.
    """
    prefix = _privileged_prefix(ssh_user)
    status, stdout, _ = _run_remote(
        ssh,
        f"{prefix}sh -c 'for t in wg qrencode; do"
        " command -v $t >/dev/null 2>&1 || echo $t; done'",
    )
    if status != 0:
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _packages_to_install(ssh: paramiko.SSHClient, ssh_user: str) -> tuple[str, ...]:
    """Work out which packages a successful install is about to add.

    Runs before the installer, so a later destroy has provenance instead of a
    guess: only packages that were missing are reported.

    Args:
        ssh (paramiko.SSHClient): Connected SSH client.
        ssh_user (str): Remote username (root skips ``sudo``).

    Returns:
        tuple[str, ...]: Package names for the detected family, deduplicated and
        in install order. Empty when the family is unknown or every tool is
        already present.
    """
    family = _detect_package_family(ssh, ssh_user)
    if not family:
        logger.debug("Package family not detected; package provenance disabled")
        return ()

    packages: list[str] = []
    for tool in _missing_tool_commands(ssh, ssh_user):
        packages.extend(_PACKAGES_BY_FAMILY.get(family, {}).get(tool, ()))
    return tuple(dict.fromkeys(packages))


def _write_installed_packages(
    ssh: paramiko.SSHClient, ssh_user: str, packages: tuple[str, ...]
) -> bool:
    """Record the packages Wisp installed in :data:`INSTALLED_PACKAGES_PATH`.

    The manifest is the destroy path's provenance. It is written only after a
    successful install and only with packages that were missing beforehand, so
    removing them cannot take away tooling the server already had.

    Args:
        ssh (paramiko.SSHClient): Connected SSH client.
        ssh_user (str): Remote username (root skips ``sudo``).
        packages (tuple[str, ...]): Package names to record.

    Returns:
        bool: True when the manifest was written (or there was nothing to
        record); False when the write failed.
    """
    if not packages:
        return True

    prefix = _privileged_prefix(ssh_user)
    listed = " ".join(packages)
    command = (
        f"{prefix}sh -c 'mkdir -p /etc/wisp"
        f' && printf "%s\\n" {listed} > {INSTALLED_PACKAGES_PATH}'
        f" && chmod 600 {INSTALLED_PACKAGES_PATH}'"
    )
    status, _, err = _run_remote(ssh, command)
    if status != 0:
        logger.warning(
            "Could not record the installed packages on the server: "
            f"{err.strip() or 'unknown error'}"
        )
        return False

    logger.debug(f"Recorded Wisp-installed packages: {', '.join(packages)}")
    return True


def _connect_ssh(
    ssh_user: str, instance_ip: str, ssh_key_file: str
) -> paramiko.SSHClient:
    """Open an SSH connection to a server, authenticated by public key.

    Tries to load the private key as Ed25519, ECDSA, then RSA (OpenSSH or
    PKCS#8). When ``ssh_key_file`` is empty, connects without an explicit key
    so Paramiko falls back to the SSH agent and default key locations.

    Args:
        ssh_user (str): User to connect as.
        instance_ip (str): Server hostname or IP.
        ssh_key_file (str): Path to the user's private key, or ``""`` for
            agent/default-key authentication.

    Returns:
        paramiko.SSHClient: The connected client; the caller owns it.

    Raises:
        paramiko.SSHException: When the key cannot be loaded or the
            connection fails.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key = None
    if ssh_key_file:
        for key_class in (
            paramiko.Ed25519Key,
            paramiko.ECDSAKey,
            paramiko.RSAKey,
        ):
            try:
                key = key_class.from_private_key_file(str(ssh_key_file))
                logger.debug(f"Loaded SSH key as {key_class.__name__}")
                break
            except paramiko.SSHException:
                continue
        if key is None:
            raise paramiko.SSHException(
                f"Unable to load private key from {ssh_key_file}. "
                "Supported formats: Ed25519, ECDSA, RSA (OpenSSH or PKCS#8)"
            )

    try:
        ssh.connect(
            hostname=instance_ip,
            username=ssh_user,
            pkey=key,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
    except Exception:
        ssh.close()
        raise
    logger.debug(f"SSH connection established to {ssh_user}@{instance_ip}")
    return ssh


def _run_remote(ssh: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    """Run a command on the server and return its exit status and output.

    Args:
        ssh (paramiko.SSHClient): The connected SSH client.
        command (str): Shell command to execute.

    Returns:
        tuple[int, str, str]: Exit status, stdout, and stderr.
    """
    _, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, stdout.read().decode(), stderr.read().decode()


def remove_wisp_server_configs(
    ssh_user: str,
    instance_ip: str,
    ssh_key_file: str = "",
    on_progress: Callable[[str, float | None], None] | None = None,
    also_remove_params: bool = False,
) -> bool:
    """Tear down every Wisp-created WireGuard interface on a remote server.

    Wisp names its server-side interfaces ``wisp-<hex>``, so only files
    matching ``/etc/wireguard/wisp-*.conf`` are touched — a pre-existing
    ``wg0.conf`` (or any non-Wisp config) is left alone. For each match the
    interface is brought down first: ``wg-quick down`` runs the config's
    ``PostDown`` rules, which removes the iptables/firewall rules the
    installer added. The generated client configs (which contain the
    client's private key) are removed from the server too. Packages are not
    touched here — the server may have been using WireGuard before Wisp;
    :func:`remove_installed_packages` removes only the ones a manifest proves
    Wisp introduced, and only when no other config remains.

    Everything runs through ``sudo -n`` unless connecting as root:
    ``/etc/wireguard`` is root-only on many distros, so an unprivileged
    glob finds nothing and ``wg-quick``/``rm`` would fail. ``-n`` fails
    fast instead of hanging when a password would be required.

    Args:
        ssh_user (str): User to connect as (needs sudo/root).
        instance_ip (str): Server hostname or IP.
        ssh_key_file (str): Path to the user's private key, or ``""`` for
            agent/default-key authentication.
        on_progress (Callable[[str, float | None], None] | None): Optional
            progress callback.
        also_remove_params (bool): Also delete ``/etc/wireguard/params`` —
            the installer's state file, which would otherwise trigger its
            interactive manage menu on a re-deploy. Deploy paths pass True
            unconditionally: a successful install overwrites params anyway.
            Destroy paths leave it (they may run against servers whose
            params file predates Wisp).

    Returns:
        bool: True when the cleanup ran (even with zero matches); False when
        the SSH connection or the cleanup commands failed.
    """

    def _report(message: str, progress: float | None = None) -> None:
        if on_progress is not None:
            on_progress(message, progress)

    prefix = "" if ssh_user == "root" else "sudo -n "
    _report("Connecting to server over SSH...")
    try:
        ssh = _connect_ssh(ssh_user, instance_ip, ssh_key_file)
    except Exception as exc:
        logger.warning(f"Could not SSH to {instance_ip} for cleanup: {exc}")
        _report(f"Server cleanup skipped (SSH failed): {exc}", 1.0)
        return False

    try:
        _report("Removing Wisp WireGuard config from the server...", 0.6)
        # The glob must expand inside a privileged shell: /etc/wireguard is
        # often 0700 root, so an unprivileged expansion matches nothing.
        exit_status, listing, list_err = _run_remote(
            ssh, f"{prefix}sh -c 'echo /etc/wireguard/wisp-*.conf'"
        )
        listing = listing.strip()
        nics = []
        if exit_status == 0 and listing and "*" not in listing:
            nics = [
                path.removeprefix("/etc/wireguard/").removesuffix(".conf")
                for path in listing.splitlines()
                if path.strip()
            ]
        elif exit_status != 0 and ssh_user != "root":
            # A zero exit with an unmatched glob just means "no Wisp
            # configs"; only a real command failure is worth warning about.
            logger.warning(
                f"Could not list /etc/wireguard as {ssh_user}@{instance_ip} "
                f"(sudo unavailable or password required): {list_err.strip()}"
            )

        for nic in nics:
            logger.info(f"Tearing down Wisp interface '{nic}' on {instance_ip}")
            # Best effort per interface: a missing/failed step for one config
            # must not stop the rest of the cleanup.
            # Disarm the in-VM watchdog first: a destroyed session must not
            # leave a timer behind that fires against an interface which no
            # longer exists.
            _run_remote(
                ssh,
                f"{prefix}systemctl disable --now wisp-watchdog@{nic}.timer "
                "2>/dev/null || true",
            )
            _run_remote(ssh, f"{prefix}rm -f /etc/wisp/watchdog-{nic}.conf")
            _run_remote(ssh, f"{prefix}wg-quick down {nic} 2>/dev/null || true")
            _run_remote(
                ssh,
                f"{prefix}systemctl disable --now wg-quick@{nic} 2>/dev/null || true",
            )
            _run_remote(ssh, f"{prefix}rm -f /etc/wireguard/{nic}.conf")

        if also_remove_params:
            # Stale installer state would trigger the manage menu (a
            # blocking prompt) on the next deploy — even when no Wisp config
            # is present anymore (destroy already removed it).
            _run_remote(ssh, f"{prefix}rm -f /etc/wireguard/params")

        if nics:
            # The generated client confs hold the client's private key; they
            # must not linger on the server (root's and the SSH user's home).
            _run_remote(
                ssh,
                f"{prefix}sh -c 'rm -f /root/wisp-*-client-*.conf"
                f" /home/{ssh_user}/wisp-*-client-*.conf'",
            )
            # If the installer state file belongs to a Wisp interface, remove
            # it too — a destroyed session must not leave it behind. A params
            # file referencing the user's own interface survives untouched.
            _run_remote(
                ssh,
                f'{prefix}sh -c \'grep -q "^SERVER_WG_NIC=wisp-"'
                " /etc/wireguard/params 2>/dev/null"
                " && rm -f /etc/wireguard/params' || true",
            )
            # The watchdog script and its unit templates are shared by every
            # session on the server: remove them only once the last watchdog
            # config is gone, so another session's timer keeps working.
            _run_remote(
                ssh,
                f"{prefix}sh -c 'ls /etc/wisp/watchdog-*.conf >/dev/null 2>&1 || {{"
                " rm -f /etc/systemd/system/wisp-watchdog@.service"
                " /etc/systemd/system/wisp-watchdog@.timer"
                " /usr/local/bin/wisp-watchdog;"
                " systemctl daemon-reload;"
                " rmdir /etc/wisp 2>/dev/null; }}'",
            )
            _report(f"Removed {len(nics)} Wisp interface(s) from the server.", 0.9)
        else:
            _report("No Wisp interfaces found on the server.", 0.9)
        return True
    finally:
        ssh.close()
        logger.debug("SSH connection closed")


def remove_installed_packages(
    ssh_user: str,
    instance_ip: str,
    ssh_key_file: str = "",
    on_progress: Callable[[str, float | None], None] | None = None,
) -> bool:
    """Remove the packages Wisp installed on a server the user already owned.

    Custom VPS deployments install WireGuard tooling on a machine that keeps
    existing after the session. :func:`configure_remote_server` records the
    packages that were missing before installing in
    :data:`INSTALLED_PACKAGES_PATH`, so this removes exactly what Wisp
    introduced and leaves tooling the server already had.

    Only packages in :data:`REMOVABLE_PACKAGES` are removed. ``iptables`` and
    ``resolvconf`` are never touched even when Wisp installed them: a server
    may depend on them for its own firewall or DNS, and Wisp does not own it.

    Removal is skipped entirely when another config remains in
    ``/etc/wireguard`` after the Wisp ones were deleted. That means the user
    set up their own tunnel on that server, so the tooling now serves
    something beyond Wisp — the same ownership rule the tunnel teardown uses.

    Everything runs through ``sudo -n`` unless connecting as root; ``-n``
    fails fast instead of hanging when a password would be required.

    Args:
        ssh_user (str): User to connect as (needs sudo/root).
        instance_ip (str): Server hostname or IP.
        ssh_key_file (str): Path to the user's private key, or ``""`` for
            agent/default-key authentication.
        on_progress (Callable[[str, float | None], None] | None): Optional
            progress callback.

    Returns:
        bool: True when the cleanup ran, including the deliberate skips (no
        manifest, foreign config present, unknown package manager). False when
        the SSH connection, the removal, or the manifest read failed.
    """

    def _report(message: str, progress: float | None = None) -> None:
        if on_progress is not None:
            on_progress(message, progress)

    prefix = _privileged_prefix(ssh_user)
    _report("Checking WireGuard packages on the server...")
    try:
        ssh = _connect_ssh(ssh_user, instance_ip, ssh_key_file)
    except Exception as exc:
        logger.warning(f"Could not SSH to {instance_ip} for package cleanup: {exc}")
        _report(f"Package cleanup skipped (SSH failed): {exc}", 1.0)
        return False

    try:
        # A config Wisp does not own means its WireGuard tooling is in use
        # beyond this session. Match the same ``wisp-*.conf`` ownership rule
        # the teardown uses, so only Wisp's own files are excluded.
        _, listing, _ = _run_remote(
            ssh,
            f"{prefix}sh -c 'ls /etc/wireguard/*.conf 2>/dev/null"
            " | grep -v '/wisp-[^/]*\\.conf$' || true'",
        )
        foreign = [line.strip() for line in listing.splitlines() if line.strip()]
        if foreign:
            logger.info(
                f"Keeping WireGuard packages on {instance_ip}: the server has "
                f"its own config ({foreign[0]})"
            )
            _report("Keeping WireGuard packages: the server has its own tunnel.", 1.0)
            return True

        status, manifest, err = _run_remote(
            ssh, f"{prefix}cat {INSTALLED_PACKAGES_PATH} 2>/dev/null"
        )
        if status != 0:
            logger.warning(
                f"Could not read {INSTALLED_PACKAGES_PATH} on {instance_ip}: "
                f"{err.strip() or 'unknown error'}"
            )
            _report("Package cleanup skipped (could not read the manifest).", 1.0)
            return False
        if not manifest.strip():
            _report("No Wisp-installed packages recorded; nothing to remove.", 1.0)
            return True

        recorded = [line.strip() for line in manifest.splitlines() if line.strip()]
        removable = [name for name in recorded if name in REMOVABLE_PACKAGES]
        kept = [name for name in recorded if name not in REMOVABLE_PACKAGES]
        if kept:
            logger.info(
                f"Leaving {', '.join(kept)} installed on {instance_ip}: the "
                "installer adds them, but they may serve the server itself"
            )
        if not removable:
            _report("Nothing to remove: recorded packages are kept by design.", 1.0)
            return True

        family = _detect_package_family(ssh, ssh_user)
        removal = _REMOVAL_COMMANDS.get(family)
        if removal is None:
            logger.warning(
                f"Could not detect a package manager on {instance_ip}; leaving "
                f"{', '.join(removable)} installed"
            )
            _report("Packages left installed (unknown package manager).", 1.0)
            return True

        _report(f"Removing packages Wisp installed ({', '.join(removable)})...", 0.5)
        status, _, err = _run_remote(ssh, f"{prefix}{removal} {' '.join(removable)}")
        if status != 0:
            # Keep the manifest so a later destroy can retry.
            logger.warning(
                f"Removing packages on {instance_ip} failed: "
                f"{err.strip() or 'unknown error'}"
            )
            _report(
                f"Package removal failed: {err.strip() or 'see the server log'}",
                1.0,
            )
            return False

        _run_remote(
            ssh,
            f"{prefix}sh -c 'rm -f {INSTALLED_PACKAGES_PATH};"
            " rmdir /etc/wisp 2>/dev/null || true'",
        )
        logger.info(
            f"Removed Wisp-installed packages on {instance_ip}: {', '.join(removable)}"
        )
        _report(f"Removed {len(removable)} package(s) Wisp installed.", 1.0)
        return True
    except Exception as exc:
        # Best effort by contract: a cleanup problem must never turn a
        # successful tunnel teardown into a failed destroy.
        logger.warning(f"Package cleanup on {instance_ip} failed: {exc}")
        _report(f"Package cleanup skipped: {exc}", 1.0)
        return False
    finally:
        ssh.close()
        logger.debug("SSH connection closed")


def ensure_persistent_keepalive(conf_path: Path) -> bool:
    """Ensure PersistentKeepalive = 25 is present in the WireGuard client config.

    The in-VM watchdog decides whether a session is abandoned from the age of
    the last WireGuard handshake. Without keepalive packets, an idle-but-healthy
    tunnel emits no handshakes and would be killed by mistake.

    Args:
        conf_path (Path): Path to the WireGuard client configuration file.

    Returns:
        bool: True if ``PersistentKeepalive = 25`` was added, False if it was
        already present, the ``[Peer]`` section was missing, or an error occurred.

    Raises:
        None: Never raises; errors are logged as debug messages and return False.
    """
    try:
        if not conf_path.is_file():
            logger.debug(
                f"Client configuration file not found for keepalive check: {conf_path}"
            )
            return False

        content = conf_path.read_text(encoding="utf-8")

        # Check if PersistentKeepalive already exists (case-insensitive key match)
        if re.search(
            r"^\s*persistentkeepalive\s*=", content, re.IGNORECASE | re.MULTILINE
        ):
            return False

        # Verify [Peer] section exists
        if not re.search(r"^\s*\[peer\]\s*$", content, re.IGNORECASE | re.MULTILINE):
            logger.debug(
                f"No [Peer] section found in client configuration: {conf_path}"
            )
            return False

        separator = "" if content.endswith("\n") else "\n"
        new_content = f"{content}{separator}PersistentKeepalive = 25\n"
        conf_path.write_text(new_content, encoding="utf-8")
        logger.debug(f"Added PersistentKeepalive = 25 to {conf_path}")
        return True
    except Exception as exc:
        logger.debug(f"Failed to ensure PersistentKeepalive in {conf_path}: {exc}")
        return False


def build_watchdog_config(
    nic: str,
    mode: str,
    ttl_minutes: int,
    idle_seconds: int,
    server_started_epoch: int,
) -> str:
    """Build the configuration file content for the in-VM session watchdog.

    Args:
        nic (str): WireGuard interface name (e.g. ``"wg0"`` or ``"wisp-abc"``).
        mode (str): Watchdog mode (``"cloud"`` or ``"vps"``).
        ttl_minutes (int): Hard session TTL cap in minutes (0 to disable).
        idle_seconds (int): Maximum seconds without handshake before idle teardown.
        server_started_epoch (int): Server timestamp (epoch seconds) when started.

    Returns:
        str: The content for the watchdog configuration file.
    """
    deadline_epoch = server_started_epoch + ttl_minutes * 60 if ttl_minutes > 0 else 0
    return (
        f'NIC="{nic}"\n'
        f'MODE="{mode}"\n'
        f"IDLE_SECONDS={idle_seconds}\n"
        f"DEADLINE_EPOCH={deadline_epoch}\n"
        f"SESSION_STARTED_EPOCH={server_started_epoch}\n"
    )


def install_session_watchdog(
    ssh: paramiko.SSHClient,
    *,
    nic: str,
    mode: str,
    ttl_minutes: int,
    idle_seconds: int,
    ssh_user: str,
) -> bool:
    """Install and enable the in-VM session watchdog systemd timer on the remote server.

    Provisions an autonomous dead man's switch inside the server to self-destruct
    an abandoned session even if the user's local machine shuts down or disconnects.

    Args:
        ssh (paramiko.SSHClient): Connected SSH client for the remote server.
        nic (str): WireGuard interface name (e.g. ``"wg0"`` or ``"wisp-abc"``).
        mode (str): Watchdog mode (``"cloud"`` or ``"vps"``).
        ttl_minutes (int): Hard session TTL cap in minutes (0 to disable).
        idle_seconds (int): Maximum seconds without handshake before idle teardown.
        ssh_user (str): Remote username with sudo privileges.

    Returns:
        bool: True if the watchdog timer was successfully installed and enabled,
        False on any failure.

    Raises:
        None: Never raises; errors are logged as warnings and return False.
    """
    try:
        if not re.match(r"^[a-zA-Z0-9_-]+$", nic):
            logger.warning(
                f"Refusing to install session watchdog: invalid interface name '{nic}'"
            )
            return False

        # Read the current epoch from the server's clock to avoid client/server skew
        exit_status, stdout, stderr = _run_remote(ssh, "date +%s")
        if exit_status != 0:
            logger.warning(f"Failed to read remote server timestamp: {stderr.strip()}")
            return False

        try:
            server_now = int(stdout.strip())
        except ValueError:
            logger.warning(f"Unexpected date output from server: '{stdout.strip()}'")
            return False

        session_started_epoch = server_now

        remote_script_path = "/tmp/wisp-watchdog"
        remote_conf_path = f"/tmp/wisp-watchdog-{nic}.conf"
        service_unit_path = "/tmp/wisp-watchdog@.service"
        timer_unit_path = "/tmp/wisp-watchdog@.timer"

        # Upload script, configuration file, and systemd units via SFTP
        sftp = ssh.open_sftp()
        try:
            sftp.put(str(WATCHDOG_SCRIPT_PATH), remote_script_path)
            sftp.chmod(
                remote_script_path,
                stat.S_IRWXU
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH,
            )

            conf_content = build_watchdog_config(
                nic=nic,
                mode=mode,
                ttl_minutes=ttl_minutes,
                idle_seconds=idle_seconds,
                server_started_epoch=session_started_epoch,
            )
            with sftp.file(remote_conf_path, "w") as conf_file:
                conf_file.write(conf_content)
            sftp.chmod(remote_conf_path, stat.S_IRUSR | stat.S_IWUSR)

            service_unit_content = (
                "[Unit]\n"
                "Description=Wisp session watchdog (%i)\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                "ExecStart=/usr/local/bin/wisp-watchdog %i\n"
            )
            with sftp.file(service_unit_path, "w") as svc_file:
                svc_file.write(service_unit_content)
            sftp.chmod(
                service_unit_path,
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
            )

            timer_unit_content = (
                "[Unit]\n"
                "Description=Wisp session watchdog timer (%i)\n\n"
                "[Timer]\n"
                "OnBootSec=60\n"
                "OnUnitActiveSec=60\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            )
            with sftp.file(timer_unit_path, "w") as timer_file:
                timer_file.write(timer_unit_content)
            sftp.chmod(
                timer_unit_path,
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
            )
        finally:
            sftp.close()

        prefix = "" if ssh_user == "root" else "sudo -n "
        install_cmd = (
            f"{prefix}mkdir -p /etc/wisp && "
            f"{prefix}install -m 0755 {remote_script_path} /usr/local/bin/wisp-watchdog && "
            f"{prefix}install -m 0600 {remote_conf_path} /etc/wisp/watchdog-{nic}.conf && "
            f"{prefix}install -m 0644 {service_unit_path} /etc/systemd/system/wisp-watchdog@.service && "
            f"{prefix}install -m 0644 {timer_unit_path} /etc/systemd/system/wisp-watchdog@.timer && "
            f"{prefix}systemctl daemon-reload && "
            f"{prefix}systemctl enable --now wisp-watchdog@{nic}.timer"
        )
        exit_status, stdout, stderr = _run_remote(ssh, install_cmd)

        # Clean up temporary staging files from /tmp best-effort
        _run_remote(
            ssh,
            f"rm -f {remote_script_path} {remote_conf_path} "
            f"{service_unit_path} {timer_unit_path}",
        )

        if exit_status != 0:
            logger.warning(
                f"Failed to install systemd watchdog units for '{nic}': {stderr.strip()}"
            )
            return False

        logger.info(
            f"In-server session watchdog installed and armed for '{nic}' "
            f"(mode={mode}, ttl_minutes={ttl_minutes}, idle_seconds={idle_seconds})"
        )
        return True
    except Exception as exc:
        logger.warning(
            f"Unexpected error installing session watchdog for '{nic}': {exc}"
        )
        return False


def configure_remote_server(
    inventory_context: InventoryContext,
    on_progress: Callable[[str, float | None], None] | None = None,
) -> None:
    """
    Configure the remote WireGuard server using Paramiko (SSH/SFTP).

    Copies the install script to the remote server, executes it with the
    provided configuration via environment variables, and downloads the
    generated client configuration.

    Args:
        inventory_context (InventoryContext): Context containing variables for
            the WireGuard installation.
        on_progress (Callable[[str, float | None], None] | None): Optional
            callback receiving human-readable progress updates (message, and a
            percentage, or ``None`` while indeterminate). The installer output is
            streamed through it, throttled to one update per second, so a long
            install never looks like a hang.
    """

    # A namespace object can't be refreshed after `paramiko` connect; each
    # phase reports through this so screen code stays fully decoupled.
    def _report(message: str, progress: float | None = None) -> None:
        if on_progress is not None:
            on_progress(message, progress)

    ssh_user = inventory_context["ssh_user"]
    instance_ip = inventory_context["instance_ip"]
    ssh_key_file = inventory_context["ssh_key_file"]
    wireguard_port = inventory_context["wireguard_port"]
    allowed_ips = inventory_context["allowed_ips"]
    wireguard_public_ip = inventory_context["wireguard_public_ip"]
    wireguard_interface = inventory_context["wireguard_interface"]
    wireguard_ipv4 = inventory_context["wireguard_ipv4"]
    wireguard_ipv6 = inventory_context["wireguard_ipv6"]
    wireguard_dns1 = inventory_context["wireguard_dns1"]
    wireguard_dns2 = inventory_context["wireguard_dns2"]
    wireguard_client_name = inventory_context["wireguard_client_name"]
    wireguard_client_ipv4 = inventory_context["wireguard_client_ipv4"]
    wireguard_client_ipv6 = inventory_context["wireguard_client_ipv6"]
    wireguard_skip_client = inventory_context["wireguard_skip_client"]

    logger.info(f"Configuring remote WireGuard server at {instance_ip} via SSH...")

    # Prepare environment variables for the remote script
    env_vars = {
        "SERVER_PUB_IP": wireguard_public_ip,
        "SERVER_PORT": str(wireguard_port),
        "ALLOWED_IPS": allowed_ips,
        "SERVER_WG_NIC": wireguard_interface,
        "SERVER_WG_IPV4": wireguard_ipv4,
        "SERVER_WG_IPV6": wireguard_ipv6,
        "CLIENT_DNS_1": wireguard_dns1,
        "CLIENT_DNS_2": wireguard_dns2,
        "AUTO_INSTALL": "y",
        "CLIENT_NAME": wireguard_client_name,
        "CLIENT_WG_IPV4": wireguard_client_ipv4,
        "CLIENT_WG_IPV6": wireguard_client_ipv6,
        "SKIP_CLIENT_CREATION": wireguard_skip_client,
    }

    ssh = _connect_ssh(ssh_user, instance_ip, str(ssh_key_file) if ssh_key_file else "")
    _report("Connected to server over SSH.")

    # Provenance for the destroy path: record what this install adds *before*
    # it runs, so a later teardown removes only Wisp's own packages. A probe
    # failure must never block the deploy — it only disables cleanup.
    try:
        wisp_installed_packages = _packages_to_install(ssh, ssh_user)
    except Exception as exc:
        logger.warning(f"Could not probe existing packages on {instance_ip}: {exc}")
        wisp_installed_packages = ()

    try:
        # Upload the install script via SFTP
        _report("Uploading WireGuard installer to the server...")
        sftp = ssh.open_sftp()
        remote_script_path = "/tmp/wireguard-install.sh"
        sftp.put(str(WIREGUARD_SCRIPT_PATH), remote_script_path)

        # Make script executable
        sftp.chmod(
            remote_script_path,
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
        )
        sftp.close()
        logger.debug(f"Uploaded install script to {remote_script_path}")

        # Build the command with environment variables
        env_prefix = " ".join(f"{k}={v}" for k, v in env_vars.items())
        command = f"sudo {env_prefix} {remote_script_path}"
        logger.debug(f"Executing remote command: {command}")
        _report("Installing WireGuard on the server (this may take several minutes)...")

        # Execute the script with a pseudo-terminal for sudo. The channel
        # read timeout (10 min) bounds a hung install: the installer streams
        # output continuously, so 10 quiet minutes almost always mean it is
        # blocked on an interactive prompt.
        stdin, stdout, stderr = ssh.exec_command(command, get_pty=True, timeout=600)

        # Stream the installer output in real-time. `recv_exit_status()` blocks
        # until the script exits and delivers *no* output while running, so a
        # minutes-long install would otherwise sit frozen on one status line.
        # Read lines as they arrive instead, throttling UI updates to one per
        # second (the installer floods progress bars for apt-get, etc.).
        last_report: float = time.monotonic()

        def _stream_installer_output() -> None:
            nonlocal last_report
            for raw in stdout:
                raw_text = (
                    raw.decode(errors="replace") if isinstance(raw, bytes) else raw
                )
                line = re.sub(
                    r"\x1b\[[0-9;?]*[A-Za-z]",
                    "",
                    raw_text.replace("\r", ""),
                ).strip()
                if not line:
                    continue
                # Kept in memory for the failure message below; raw lines are
                # deliberately NOT logged — they are pure apt progress noise.
                seen_lines.append(line)
                now = time.monotonic()
                if now - last_report >= 1.0:
                    last_report = now
                    _report(line, None)

        seen_lines: list[str] = []
        try:
            _stream_installer_output()
        except socket.timeout as exc:
            # The channel read timeout fired: no installer output for many
            # minutes. It is almost certainly blocked on an interactive
            # prompt — surface the tail so the prompt itself is visible.
            tail = "\n".join(seen_lines[-15:])
            raise RuntimeError(
                "The WireGuard installer produced no output for 10 minutes — "
                "it is likely waiting for input on the server.\n"
                f"Last installer output:\n{tail}"
            ) from exc
        exit_status = stdout.channel.recv_exit_status()
        stderr_text = stderr.read().decode()

        if exit_status != 0:
            # Keep the last lines around: they usually carry the actual
            # apt/shell error when the install fails.
            tail = "\n".join(seen_lines[-30:])
            raise RuntimeError(
                f"WireGuard installation script failed with exit code {exit_status}. "
                f"Stderr: {stderr_text}\nLast installer output:\n{tail}"
            )

        logger.info("WireGuard server installation completed successfully")

        # Only now are the probed packages actually on disk. Best effort:
        # without a manifest a destroy simply leaves them installed.
        _write_installed_packages(ssh, ssh_user, wisp_installed_packages)

        # Verify the interface actually came up: the installer only warns
        # when wg-quick fails to start, and reporting a dead tunnel as a
        # successful deploy would leave the user debugging silence.
        verify = (
            "sh -c 'systemctl is-active --quiet wg-quick@%s 2>/dev/null"
            " || rc-service --quiet wg-quick.%s status 2>/dev/null'"
            % (wireguard_interface, wireguard_interface)
        )
        verify_status, _, verify_err = _run_remote(ssh, f"sudo -n {verify}")
        if verify_status != 0:
            raise RuntimeError(
                f"WireGuard interface '{wireguard_interface}' is not running "
                f"on the server (systemd/rc service not active): "
                f"{verify_err.strip()}. Check 'journalctl -u "
                f"wg-quick@{wireguard_interface}' on the server."
            )
        logger.info(
            f"WireGuard interface '{wireguard_interface}' is active on the server"
        )

        # Wait a moment for the client config file to be created
        time.sleep(2)
        _report("Downloading client configuration...")

        # Download the client configuration via SFTP
        sftp = ssh.open_sftp()
        remote_conf_pattern = f"/root/{wireguard_interface}-client-*.conf"
        logger.debug(f"Looking for client config: {remote_conf_pattern}")

        # Find the generated client config file
        stdin, stdout, stderr = ssh.exec_command(
            f"ls -t {remote_conf_pattern} 2>/dev/null | head -1"
        )
        remote_conf_path = stdout.read().decode().strip()

        if not remote_conf_path:
            # Fallback: check home directory of ssh_user
            stdin, stdout, stderr = ssh.exec_command(
                f"ls -t /home/{ssh_user}/{wireguard_interface}-client-*.conf 2>/dev/null | head -1"
            )
            remote_conf_path = stdout.read().decode().strip()

        if not remote_conf_path:
            raise FileNotFoundError(
                "WireGuard client configuration file not found on remote server"
            )

        logger.debug(f"Downloading client config from {remote_conf_path}")

        Path(WIREGUARD_CLIENT_CONF_PATH).parent.mkdir(parents=True, exist_ok=True)
        sftp.get(remote_conf_path, str(WIREGUARD_CLIENT_CONF_PATH))
        sftp.close()

        logger.info(f"Client configuration downloaded to {WIREGUARD_CLIENT_CONF_PATH}")

        # Ensure PersistentKeepalive is set so an idle client sends keepalives,
        # preventing the in-VM watchdog from falsely flagging a healthy session as abandoned.
        ensure_persistent_keepalive(WIREGUARD_CLIENT_CONF_PATH)

        # Install and arm the in-VM session watchdog
        _report("Arming the in-server session watchdog...", 0.95)
        try:
            install_session_watchdog(
                ssh,
                nic=wireguard_interface,
                mode=inventory_context.get("watchdog_mode", "cloud"),
                ttl_minutes=int(inventory_context.get("watchdog_ttl_minutes", 0)),
                idle_seconds=int(inventory_context.get("watchdog_idle_seconds", 900)),
                ssh_user=ssh_user,
            )
        except Exception as exc:
            logger.warning(f"Could not install session watchdog: {exc}")

    finally:
        ssh.close()
        logger.debug("SSH connection closed")
