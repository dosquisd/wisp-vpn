"""Common validation and resolution helpers for CLI commands."""

import sys

from wisp.cli.args import RuntimeArgs
from wisp.config.settings import WISP_USER_CONFIG_PATH
from wisp.providers import ProviderEnum
from wisp.providers.base import BaseProvider


def require_provider(args: RuntimeArgs) -> ProviderEnum:
    """Validate and return the provider from runtime arguments.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        ProviderEnum: Specified provider.
    """
    provider = args["provider"]
    assert provider is not None, (
        "Provider must be specified for deploy, destroy, or regions commands."
    )
    return provider


def resolve_region(
    args: RuntimeArgs, provider_enum: ProviderEnum, provider: BaseProvider
) -> str | None:
    """Resolve deployment/destroy region from arguments or provider credentials.

    Args:
        args: Runtime arguments parsed from the CLI.
        provider_enum: Target provider enum.
        provider: Instantiated provider.

    Returns:
        str | None: Resolved region string, or None if missing for cloud providers.
    """
    region = args["region"] or getattr(provider.credentials, "region", None)
    if provider_enum != ProviderEnum.VPS and not region:
        print(
            f"No region configured for {provider_enum.value}. Pass -r/--region, "
            f"or set it under the [{provider_enum.value}] section of "
            f"{WISP_USER_CONFIG_PATH}.",
            file=sys.stderr,
        )
        return None
    return region
