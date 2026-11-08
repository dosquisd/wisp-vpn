"""Provider factory and connection resolution services."""

from typing import Any

from wisp.config.credentials import (
    VPSCredentials,
    resolve_aws_credentials,
    resolve_gcp_credentials,
    resolve_oci_credentials,
    resolve_vps_credentials,
)
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.providers.base import BaseProvider


def create_provider(provider: ProviderEnum) -> BaseProvider:
    """Create a provider instance with resolved credentials.

    Args:
        provider: Target cloud provider enum.

    Returns:
        BaseProvider: Instantiated provider with resolved credentials.
    """
    if provider == ProviderEnum.AWS:
        creds = resolve_aws_credentials()
        return PROVIDERS_MAP[provider](credentials=creds)
    elif provider == ProviderEnum.OCI:
        creds = resolve_oci_credentials()
        return PROVIDERS_MAP[provider](credentials=creds)
    elif provider == ProviderEnum.GCP:
        creds = resolve_gcp_credentials()
        return PROVIDERS_MAP[provider](credentials=creds)
    elif provider == ProviderEnum.VPS:
        creds = resolve_vps_credentials()
        return PROVIDERS_MAP[provider](credentials=creds)
    else:
        return PROVIDERS_MAP[provider]()


def resolve_vps_connection(
    overrides: dict[str, Any] | None = None,
    *,
    fallback: dict[str, Any] | None = None,
) -> VPSCredentials:
    """Resolve VPS connection details with fallback and override precedence.

    Precedence: overrides > fallback > TOML.

    Args:
        overrides: CLI flag overrides.
        fallback: Fallback connection details (e.g. from active session marker).

    Returns:
        VPSCredentials: Resolved credentials object.
    """
    creds = resolve_vps_credentials()
    if fallback:
        if fallback.get("host"):
            creds.host = str(fallback["host"])
        if fallback.get("ssh_user"):
            creds.ssh_user = str(fallback["ssh_user"])
        if fallback.get("ssh_port") is not None and fallback.get("ssh_port") != "":
            creds.ssh_port = int(fallback["ssh_port"])
        if fallback.get("ssh_key_path"):
            creds.ssh_key_path = str(fallback["ssh_key_path"])

    if overrides:
        if overrides.get("host"):
            creds.host = str(overrides["host"])
        if overrides.get("ssh_user"):
            creds.ssh_user = str(overrides["ssh_user"])
        if overrides.get("ssh_port") is not None and overrides.get("ssh_port") != "":
            creds.ssh_port = int(overrides["ssh_port"])
        if overrides.get("ssh_key_path"):
            creds.ssh_key_path = str(overrides["ssh_key_path"])

    return creds


def vps_payload(credentials: VPSCredentials) -> dict[str, Any]:
    """Build the VPS payload dictionary for the active session marker.

    Args:
        credentials: VPS connection credentials.

    Returns:
        dict[str, Any]: VPS payload dictionary.
    """
    return {
        "host": credentials.host,
        "ssh_user": credentials.ssh_user,
        "ssh_port": credentials.ssh_port,
        "ssh_key_path": credentials.ssh_key_path,
    }
