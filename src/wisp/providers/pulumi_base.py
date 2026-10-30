"""Shared base class for Pulumi-based cloud providers.

Extracts common deploy/destroy logic (post-Pulumi steps) to avoid duplication
between AWS, OCI, and future providers.
"""

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TypedDict

from tqdm import tqdm

from wisp.config.constants import (
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    WIREGUARD_CLIENT_CONF_PATH,
    WIREGUARD_CLIENT_NAME,
    WIREGUARD_DNS1,
    WIREGUARD_DNS2,
    WIREGUARD_INTERFACE,
    WIREGUARD_IPV4,
    WIREGUARD_IPV6,
    WIREGUARD_KEY_PATH,
    ensure_client_conf_dir,
    ensure_keys_dir,
)
from wisp.config.settings import WispConfig, load_wisp_config
from wisp.providers.base import BaseProvider, DeployVMResult, ProgressCallback
from wisp.schemas import InventoryContext
from wisp.utils import (
    create_or_select_pulumi_stack,
    logger,
    secure_file,
)
from wisp.wireguard import (
    configure_remote_server,
    connect_wireguard_client,
    disconnect_wireguard_client,
)


class PulumiOutputs(TypedDict):
    """Expected outputs from a provider's Pulumi program."""

    instance_id: str
    instance_public_ip: str
    instance_private_ip: str
    instance_private_key: str
    wireguard_port: int
    ssh_user: str


class PulumiProvider(BaseProvider, ABC):
    """Base class for providers using Pulumi Automation API.

    Handles the common deployment flow:
    1. Run `pulumi up` with provider-specific program
    2. Wait for VM boot
    3. Write SSH/WireGuard private key
    4. Configure remote WireGuard server via SSH
    5. Connect local WireGuard client

    Subclasses must implement:
    - `_create_pulumi_program`: Build the Pulumi program closure
    - `get_available_regions`: List available regions
    - `_provider_name`: Display name for progress messages
    """

    @abstractmethod
    def _create_pulumi_program(
        self,
        region: str,
        force_current_ip: bool = False,
        wireguard_port: int | None = None,
    ) -> None:
        """Build the Pulumi program closure passed to the automation stack.

        Args:
            region: Target region.
            force_current_ip: Restrict ingress to caller's public IP.
            wireguard_port: UDP port for WireGuard; None for random.
        """
        pass

    @abstractmethod
    def get_available_regions(self) -> Sequence[str]:
        """Return the regions available for this provider."""
        pass

    @property
    @abstractmethod
    def _provider_name(self) -> str:
        """Display name for progress messages (e.g., 'AWS', 'OCI')."""
        pass

    def _get_progress_prefix(self, action: str) -> str:
        """Get a progress message prefix."""
        return f"{action} in {self._provider_name} with Pulumi..."

    def deploy_vm(
        self,
        region: str,
        force_current_ip: bool = False,
        config: WispConfig | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> DeployVMResult:
        """Provision a VM, configure WireGuard, and connect the client.

        This is the common deployment flow shared by all Pulumi-based providers.
        """
        logger.info(f"Deploying VM in region '{region}' ({self._provider_name})...")

        if config is None:
            config = load_wisp_config()
            if force_current_ip:
                config.force_current_ip = True
        else:
            force_current_ip = config.force_current_ip

        if on_progress:
            on_progress(self._get_progress_prefix("Provisioning infrastructure"), 0.05)

        logger.debug("Creating and deploying Pulumi stack...")
        stack = create_or_select_pulumi_stack(
            lambda: self._create_pulumi_program(
                region,
                force_current_ip=force_current_ip,
                wireguard_port=config.wireguard_port
                if config.wireguard_port > 0
                else None,
            ),
            provider=self._provider_name.lower(),
        )  # type: ignore
        up_result = stack.up()

        timeout_seconds = config.vm_boot_timeout
        logger.debug(
            f"Waiting for {timeout_seconds} "
            "seconds for the stack to be fully deployed..."
        )
        if on_progress:
            for elapsed in range(timeout_seconds):
                time.sleep(1)
                fraction = 0.25 + 0.50 * ((elapsed + 1) / timeout_seconds)
                on_progress(
                    f"Booting VM and SSH services ({elapsed + 1}/{timeout_seconds}s)...",
                    fraction,
                )
        else:
            for _ in tqdm(
                range(timeout_seconds),
                desc="Waiting for stack to deploy",
            ):
                time.sleep(1)

        outputs = up_result.outputs
        instance_id = outputs["instance_id"].value
        public_ip = outputs["instance_public_ip"].value
        private_ip = outputs["instance_private_ip"].value
        private_key = outputs["instance_private_key"].value
        wireguard_port = int(outputs["wireguard_port"].value)
        ssh_user = outputs["ssh_user"].value
        logger.debug(
            f"Deployed VM '{instance_id}' at '{public_ip}' with SSH user '{ssh_user}'"
        )

        if on_progress:
            on_progress("Configuring SSH keys...", 0.78)

        # Write the WireGuard/SSH private key
        ensure_keys_dir()
        WIREGUARD_KEY_PATH.write_text(private_key, encoding="utf-8")

        secure_file(WIREGUARD_KEY_PATH)
        logger.debug(
            f"Private key written to '{WIREGUARD_KEY_PATH}' with permissions 600"
        )

        # Client routing is always full-tunnel: AllowedIPs governs what the
        # client routes through the tunnel, not who can reach the server
        # (that is the firewall's job, where force_current_ip applies).
        allowed_ips = "0.0.0.0/0,::/0"

        # Build the inventory context for remote configuration
        template_context = InventoryContext(
            ssh_user=ssh_user,
            instance_ip=public_ip,
            ssh_key_file=WIREGUARD_KEY_PATH,
            wireguard_port=wireguard_port,
            allowed_ips=allowed_ips,
            wireguard_public_ip=public_ip,
            wireguard_interface=WIREGUARD_INTERFACE,
            wireguard_ipv4=WIREGUARD_IPV4,
            wireguard_ipv6=WIREGUARD_IPV6,
            wireguard_dns1=WIREGUARD_DNS1,
            wireguard_dns2=WIREGUARD_DNS2,
            wireguard_client_name=WIREGUARD_CLIENT_NAME,
            wireguard_client_ipv4="",
            wireguard_client_ipv6="",
            wireguard_skip_client="n",
            watchdog_mode="cloud",
            watchdog_ttl_minutes=config.max_session_ttl_minutes,
            watchdog_idle_seconds=DEFAULT_WATCHDOG_IDLE_SECONDS,
        )

        if on_progress:
            on_progress("Installing and configuring WireGuard over SSH...", 0.85)

        # Configure the remote WireGuard server and the local WireGuard client.
        # configure_remote_server streams its own granular progress (connect,
        # upload, live installer output, download) through the same callback.
        configure_remote_server(template_context, on_progress=on_progress)
        ensure_client_conf_dir()
        connect_response = connect_wireguard_client(
            WIREGUARD_CLIENT_CONF_PATH.read_text()
        )
        if not connect_response.ok:
            logger.warning(
                f"Failed to connect the local WireGuard client: {connect_response.message}"
            )
            if on_progress:
                on_progress(
                    "VM deployed, but the local tunnel could not connect automatically.",
                    1.0,
                )
        else:
            if on_progress:
                on_progress("VPN deployed and active!", 1.0)

        return DeployVMResult(
            instance_id=instance_id,
            public_ip=public_ip,
            private_ip=private_ip,
            wireguard_port=wireguard_port,
        )

    def delete_vm(
        self, region: str, on_progress: ProgressCallback | None = None
    ) -> int:
        """Disconnect the client, destroy the stack, and remove local artifacts.

        This is the common destruction flow shared by all Pulumi-based providers.
        """
        logger.info(f"Deleting VM in region '{region}' ({self._provider_name})...")

        if on_progress:
            on_progress("Disconnecting WireGuard client...", 0.1)
        logger.debug("Disconnecting WireGuard client...")
        disconnect_response = disconnect_wireguard_client()
        if not disconnect_response.ok:
            logger.warning(
                f"WireGuard client disconnect failed (may not be connected): {disconnect_response.message}. "
                "Continuing with stack destruction."
            )

        stack = create_or_select_pulumi_stack(
            lambda: self._create_pulumi_program(region),
            provider=self._provider_name.lower(),
        )  # type: ignore

        # If an update is still in flight (e.g. the TUI died mid-deploy and
        # the orphan cleanup is destroying the leftovers), destroy would fail
        # on the backend's update lock. Cancel is a no-op when nothing runs.
        try:
            logger.debug("Cancelling any in-flight Pulumi operation...")
            stack.cancel()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Pulumi cancel skipped: {e}")

        # The session may already be gone: the in-VM watchdog powers the VM off
        # (terminating it on AWS/GCP) as soon as the session looks abandoned. A
        # refresh reconciles the stack state with the cloud first, so destroy does
        # not fail on resources that no longer exist and does not leave the stack
        # pointing at a deleted instance. Best effort: a refresh can legitimately
        # fail (expired credentials, provider outage) and destroy must still be
        # attempted against the last known state.
        try:
            if on_progress:
                on_progress("Reconciling infrastructure state...", 0.25)
            stack.refresh()
        except Exception as exc:
            logger.warning(f"Stack refresh before destroy failed (continuing): {exc}")

        if on_progress:
            on_progress("Destroying provider infrastructure...", 0.4)
        logger.debug("Destroying Pulumi stack...")
        try:
            destroy_result = stack.destroy()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error destroying stack: {e}")
            return 0

        if on_progress:
            on_progress("Removing local keys and configuration...", 0.8)
        # Remove the WireGuard related files if they exist
        logger.debug("Removing WireGuard related files...")
        if WIREGUARD_KEY_PATH.exists():
            os.remove(WIREGUARD_KEY_PATH)
            logger.debug(f"Removed WireGuard key file: {WIREGUARD_KEY_PATH}")

        if WIREGUARD_CLIENT_CONF_PATH.exists():
            os.remove(WIREGUARD_CLIENT_CONF_PATH)
            logger.debug(
                f"Removed WireGuard client config file: {WIREGUARD_CLIENT_CONF_PATH}"
            )

        if on_progress:
            on_progress("Resources destroyed successfully.", 1.0)

        return destroy_result.summary.resource_changes.get("delete", 0)  # type: ignore
