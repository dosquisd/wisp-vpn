"""In-memory state shared across TUI screens for a single session."""

from dataclasses import dataclass, field

from wisp.config.credentials import (
    AWSCredentials,
    GCPCredentials,
    OCICredentials,
    VPSCredentials,
    resolve_aws_credentials,
    resolve_gcp_credentials,
    resolve_oci_credentials,
    resolve_vps_credentials,
)
from wisp.config.settings import (
    WispConfig,
    get_default_provider,
    get_default_region_for,
    load_wisp_config,
    reload_toml_config,
)
from wisp.providers.base import DeployVMResult, ProviderEnum, get_provider_enum
from wisp.session import SessionGuard


def _default_region_for_default_provider() -> str:
    """Region configured for the default provider (fallback: ``"us-east-2"``)."""
    return get_default_region_for(get_default_provider()) or "us-east-2"


@dataclass
class AppState:
    """Mutable per-session state held by :class:`~wisp.tui.app.WispApp`.

    Attributes:
        config (WispConfig): The active session configuration (loaded from the
            ``[general]`` TOML section).
        provider_name (str): Selected provider name.
        selected_region (str): Selected deployment region.
        last_deployment (DeployVMResult | None): Result of the most recent
            deployment, if any.
        session_guard (SessionGuard): Releases the active session's resources on
            shutdown. Armed once a tunnel is up; see :mod:`wisp.session`.
        aws_credentials (AWSCredentials): Resolved AWS credentials.
        oci_credentials (OCICredentials): Resolved OCI credentials.
        gcp_credentials (GCPCredentials): Resolved GCP credentials.
        vps_credentials (VPSCredentials): Resolved VPS connection details.
    """

    config: WispConfig = field(default_factory=load_wisp_config)
    provider_name: str = field(default_factory=get_default_provider)
    selected_region: str = field(default_factory=_default_region_for_default_provider)
    last_deployment: DeployVMResult | None = None
    session_guard: SessionGuard = field(default_factory=SessionGuard)
    aws_credentials: AWSCredentials = field(default_factory=resolve_aws_credentials)
    oci_credentials: OCICredentials = field(default_factory=resolve_oci_credentials)
    gcp_credentials: GCPCredentials = field(default_factory=resolve_gcp_credentials)
    vps_credentials: VPSCredentials = field(default_factory=resolve_vps_credentials)
    session_ttl_minutes: int | None = None

    def reset_config(self) -> None:
        """Reset :attr:`config` to a fresh :class:`WispConfig` from TOML."""
        self.config = load_wisp_config()

    def effective_session_ttl(self) -> int:
        """Return the TTL the deploy should schedule the watchdog with.

        The deploy screen sets :attr:`session_ttl_minutes` explicitly (the
        switch may disable it with ``0``); before any deploy this session it
        falls back to the configured default from ``[general]``.

        Args:
            None.

        Returns:
            int: TTL in minutes; ``0`` means the watchdog stays off.
        """
        if self.session_ttl_minutes is not None:
            return self.session_ttl_minutes
        return self.config.max_session_ttl_minutes

    def get_credentials_for_provider(self, provider_name: str):
        """Return resolved credentials for the given provider."""
        provider_enum = get_provider_enum(provider_name)
        match provider_enum:
            case ProviderEnum.AWS:
                return self.aws_credentials
            case ProviderEnum.OCI:
                return self.oci_credentials
            case ProviderEnum.GCP:
                return self.gcp_credentials
            case ProviderEnum.VPS:
                return self.vps_credentials
            case _:
                return None

    def refresh_credentials(self) -> None:
        """Re-resolve credentials from TOML/env (useful after config changes)."""
        reload_toml_config()
        self.aws_credentials = resolve_aws_credentials()
        self.oci_credentials = resolve_oci_credentials()
        self.gcp_credentials = resolve_gcp_credentials()
        self.vps_credentials = resolve_vps_credentials()
