"""Session deployment orchestration service."""

from typing import Any

from wisp.config.settings import WispConfig
from wisp.providers.base import BaseProvider, DeployVMResult, ProgressCallback
from wisp.session import write_active_session
from wisp.utils.watchdog import schedule_ttl_watchdog
from wisp.wireguard import tunnel_conflict


class TunnelConflictError(RuntimeError):
    """Raised when an existing WireGuard interface conflicts with deployment."""


def deploy_session(
    provider: BaseProvider,
    provider_name: str,
    region: str,
    config: WispConfig,
    *,
    vps_payload: dict[str, Any] | None = None,
    on_progress: ProgressCallback | None = None,
) -> DeployVMResult:
    """Deploy a VM session, record active marker, and schedule watchdog.

    Args:
        provider: Provider instance used to deploy the VM.
        provider_name: Canonical provider identifier.
        region: Target cloud region or VPS region.
        config: Wisp deployment configuration.
        vps_payload: Optional VPS connection payload for marker.
        on_progress: Optional callback for deploy progress updates.

    Returns:
        DeployVMResult: Deployed VM details.

    Raises:
        TunnelConflictError: If a WireGuard tunnel conflict is detected locally.
    """
    conflict = tunnel_conflict()
    if conflict:
        raise TunnelConflictError(conflict)

    result = provider.deploy_vm(region=region, config=config, on_progress=on_progress)

    write_active_session(
        provider_name,
        region,
        vps=vps_payload,
        ttl_minutes=config.max_session_ttl_minutes,
    )

    if config.max_session_ttl_minutes and config.max_session_ttl_minutes > 0:
        schedule_ttl_watchdog(provider_name, region, config.max_session_ttl_minutes)

    return result
