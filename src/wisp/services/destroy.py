"""Session teardown orchestration service."""

from wisp.providers.base import BaseProvider, ProgressCallback
from wisp.session import clear_active_session, read_active_session
from wisp.utils.operations import begin_teardown, end_teardown
from wisp.utils.watchdog import cancel_ttl_watchdog


def destroy_session(
    provider: BaseProvider,
    provider_name: str,
    region: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> int:
    """Tear down a deployed session, its watchdog, and active session marker.

    Args:
        provider: Provider instance used to delete the VM/tunnel.
        provider_name: Canonical provider identifier.
        region: Target cloud region or empty/vps string.
        on_progress: Optional callback for destroy progress updates.

    Returns:
        int: Count of deleted cloud resources (or 0 for VPS).
    """
    begin_teardown(provider_name, region or "")
    try:
        cancel_ttl_watchdog(provider_name, region)
        deleted = provider.delete_vm(region=region, on_progress=on_progress)
        active = read_active_session()
        if (
            active is not None
            and active.provider == provider_name
            and active.region == region
        ):
            clear_active_session()
        return deleted
    finally:
        end_teardown()
