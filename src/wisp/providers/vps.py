"""Bring-your-own-server provider: Wisp on a VPS the user already owns.

Unlike the cloud providers, nothing is provisioned here — no Pulumi, no
cloud API. The user hands Wisp an already-running Linux host reachable over
SSH, and Wisp does exactly what it does after any cloud deploy: install and
configure the WireGuard server remotely, connect the local client, and tear
everything down on demand. Packages are kept on destroy — the server may
have been using WireGuard before Wisp — but the Wisp-created interface and
its config are removed, so no tunnel is left behind.

Server-side interfaces are named ``wisp-<hex>`` (not ``wg0``) so a VPS that
already runs its own WireGuard never collides with Wisp's config; destroy
only ever touches ``/etc/wireguard/wisp-*.conf``.

Authentication is public-key based: the host's ``authorized_keys`` must
already contain the user's public key (this is how OCI and the cloud
providers bootstrap their instances too). Password authentication is
deliberately out of scope.
"""

import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from wisp.config.constants import (
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    WIREGUARD_CLIENT_CONF_PATH,
    WIREGUARD_CLIENT_NAME,
    WIREGUARD_DNS1,
    WIREGUARD_DNS2,
    WIREGUARD_IPV4,
    WIREGUARD_IPV6,
    ensure_client_conf_dir,
)
from wisp.config.credentials import VPSCredentials, resolve_vps_credentials
from wisp.config.settings import WispConfig, load_wisp_config
from wisp.providers.base import BaseProvider, DeployVMResult, ProgressCallback
from wisp.schemas import InventoryContext
from wisp.utils import get_wireguard_port
from wisp.wireguard import (
    connect_wireguard_client,
    disconnect_wireguard_client,
)
from wisp.wireguard.remote_server import (
    configure_remote_server,
    remove_wisp_server_configs,
)

# Placeholder "region" so the shared TUI flow (region Select, state, marker)
# keeps working unchanged: a user-owned VPS has no provider regions.
VPS_PLACEHOLDER_REGION = "vps"

# Prefix for server-side interface names (constraint: `^[a-zA-Z0-9_]+$`,
# < 16 chars — enforced by the installer). "wisp-" + 8 hex chars = 13.
WISP_INTERFACE_PREFIX = "wisp-"


def _new_server_interface_name() -> str:
    """Generate a unique, installer-safe server-side interface name.

    Args:
        None.

    Returns:
        str: e.g. ``"wisp-3fa9c2d1"``; collision odds are negligible and the
        destroy path matches the ``wisp-`` prefix rather than an exact name.
    """
    return f"{WISP_INTERFACE_PREFIX}{secrets.token_hex(4)}"


class VPSProvider(BaseProvider):
    """Provider for a user-owned VPS reached over SSH."""

    def __init__(self, credentials: VPSCredentials | None = None):
        """Initialize with resolved credentials or the TOML ``[vps]`` section.

        Args:
            credentials (VPSCredentials | None): Connection details; when
                ``None`` they are resolved from the TOML ``[vps]`` section.
        """
        self._credentials = credentials or resolve_vps_credentials()

    @property
    def credentials(self) -> VPSCredentials:
        """Return the VPS connection credentials."""
        return self._credentials

    @property
    def _provider_name(self) -> str:
        """Display name for progress messages."""
        return "Custom VPS"

    def get_available_regions(self) -> Sequence[str]:
        """Return the placeholder region list (a VPS has no provider regions).

        Returns:
            Sequence[str]: A single placeholder entry.
        """
        return [VPS_PLACEHOLDER_REGION]

    def deploy_vm(
        self,
        region: str,
        force_current_ip: bool = False,
        config: WispConfig | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> DeployVMResult:
        """Configure WireGuard on the user's VPS and connect the local client.

        Skips everything cloud-specific: no provisioning, no boot wait, no
        key generation (the SSH key is the user's own). The remote installer
        runs exactly as it does after a cloud deploy.

        Args:
            region (str): Ignored; the host comes from the credentials.
            force_current_ip (bool): Restrict AllowedIPs to the caller's
                current public IP (``/32``).
            config (WispConfig | None): Session config; a default is created
                if ``None``.
            on_progress (ProgressCallback | None): Optional progress callback.

        Returns:
            DeployVMResult: Host as instance/public IP, the WireGuard port
            actually in use, and the local client's private IP.

        Raises:
            ValueError: When no host is configured in ``[vps]``.
            Exception: Whatever the remote configuration or client connect
                raises on failure.
        """
        if config is None:
            config = load_wisp_config()

        creds = self._credentials
        if not creds.host:
            raise ValueError(
                "No VPS host configured. Set [vps] host (and ssh_user/"
                "ssh_port/ssh_key_path as needed) in wisp.toml."
            )
        if force_current_ip:
            config.force_current_ip = True

        def report(message: str, progress: float | None = None) -> None:
            if on_progress is not None:
                on_progress(message, progress)

        host = creds.host
        report(f"Configuring WireGuard on {host} (no provisioning needed)...", 0.1)

        # SSH key: the user's own key is used as-is (never generated/deleted).
        # When unset, fall back to the default SSH agent/discovered keys.
        key_path = Path(creds.ssh_key_path).expanduser() if creds.ssh_key_path else ""
        if key_path and not key_path.exists():
            raise FileNotFoundError(f"SSH key not found: {key_path}")

        # The installer reads the client's AllowedIPs from the inventory.
        # Client routing is always full-tunnel: AllowedIPs governs what the
        # client routes through the tunnel; the VPS has no cloud firewall
        # for Wisp to restrict (force_current_ip does not apply here).
        allowed_ips = "0.0.0.0/0,::/0"
        wireguard_port = (
            config.wireguard_port if config.wireguard_port > 0 else get_wireguard_port()
        )

        template_context = InventoryContext(
            ssh_user=creds.ssh_user,
            instance_ip=host,
            ssh_key_file=key_path if key_path else "",
            wireguard_port=wireguard_port,
            allowed_ips=allowed_ips,
            wireguard_public_ip=host,
            # Server-side interface gets a Wisp-namespaced name: the VPS may
            # already run its own wg0, and destroy only removes wisp-* files.
            wireguard_interface=_new_server_interface_name(),
            wireguard_ipv4=WIREGUARD_IPV4,
            wireguard_ipv6=WIREGUARD_IPV6,
            wireguard_dns1=WIREGUARD_DNS1,
            wireguard_dns2=WIREGUARD_DNS2,
            wireguard_client_name=WIREGUARD_CLIENT_NAME,
            wireguard_client_ipv4="",
            wireguard_client_ipv6="",
            wireguard_skip_client="n",
            watchdog_mode="vps",
            watchdog_ttl_minutes=config.max_session_ttl_minutes,
            watchdog_idle_seconds=DEFAULT_WATCHDOG_IDLE_SECONDS,
        )

        # Re-deploys: the installer detects leftover state (/etc/wireguard/
        # params) and blocks on its manage menu, and a still-running previous
        # Wisp interface would hold the subnet. Clean stale state first —
        # best effort, a fresh server simply has nothing to remove.
        remove_wisp_server_configs(
            ssh_user=creds.ssh_user,
            instance_ip=host,
            ssh_key_file=str(key_path) if key_path else "",
            on_progress=on_progress,
            also_remove_params=True,
        )

        configure_remote_server(template_context, on_progress=on_progress)

        ensure_client_conf_dir()
        connect_response = connect_wireguard_client(
            WIREGUARD_CLIENT_CONF_PATH.read_text()
        )
        if not connect_response.ok:
            report(
                "VM configured, but the local tunnel could not connect automatically.",
                1.0,
            )

        report("VPN deployed and active!", 1.0)
        return DeployVMResult(
            instance_id=host,
            public_ip=host,
            private_ip="",
            wireguard_port=wireguard_port,
        )

    def delete_vm(
        self,
        region: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Tear the tunnel down on both ends and remove local artifacts.

        Server side: every ``wisp-*`` interface is brought down and its config
        deleted (``wg-quick down`` also removes the iptables rules the
        installer added). Packages stay — the server may have been using
        WireGuard before Wisp, and a pre-existing ``wg0`` is never touched.
        Client side: the local tunnel is disconnected and the downloaded
        client config removed.

        Args:
            region (str): Ignored; present for interface compatibility.
            on_progress (ProgressCallback | None): Optional progress callback.

        Returns:
            int: 1 (the local client connection), regardless of whether it
            was connected.
        """

        def report(message: str, progress: float | None = None) -> None:
            if on_progress is not None:
                on_progress(message, progress)

        report("Disconnecting the local tunnel...", 0.2)
        disconnect_response = disconnect_wireguard_client()
        if not disconnect_response.ok:
            report(
                f"Local disconnect failed (may not be connected): "
                f"{disconnect_response.message}",
                0.4,
            )

        creds = self._credentials
        if creds.host:
            remove_wisp_server_configs(
                ssh_user=creds.ssh_user,
                instance_ip=creds.host,
                ssh_key_file=creds.ssh_key_path,
                on_progress=on_progress,
            )
        else:
            report("No VPS host configured; skipping server cleanup.", 0.3)

        report("Removing local client configuration...", 0.8)
        if WIREGUARD_CLIENT_CONF_PATH.exists():
            os.remove(WIREGUARD_CLIENT_CONF_PATH)

        report("Tunnel removed from both ends.", 1.0)
        return 1
