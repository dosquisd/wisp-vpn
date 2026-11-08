"""Destroy CLI command handler."""

import argparse
import sys

from wisp.cli.args import RuntimeArgs
from wisp.cli.commands.common import require_provider, resolve_region
from wisp.cli.options import (
    add_log_level,
    add_provider,
    add_region,
    add_vps_options,
    apply_log_level,
)
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.services.destroy import destroy_session
from wisp.services.providers import create_provider, resolve_vps_connection
from wisp.session import read_active_session
from wisp.utils.logger import logger
from wisp.utils.pulumi import describe_pulumi_error


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the destroy command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    parser = subparsers.add_parser(
        "destroy",
        help="Destroy VMs",
    )
    add_provider(parser)
    add_log_level(parser)
    add_region(
        parser,
        help_text=(
            "Region from which VMs will be destroyed "
            "(defaults to the provider's configured region)."
        ),
    )
    add_vps_options(parser, marker=True)


def run(args: RuntimeArgs) -> int:
    """Execute the destroy command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    apply_log_level(args)
    provider_enum = require_provider(args)
    provider = create_provider(provider_enum)
    region = resolve_region(args, provider_enum, provider)
    if region is None:
        return 1

    logger.info(
        f"Running in CLI mode with command: {args['command']}. "
        f"Provider: {provider_enum.value}, Region: {region}"
    )

    if provider_enum == ProviderEnum.VPS:
        marker = read_active_session()
        marker_vps = marker.vps if marker is not None else {}
        overrides = {
            "host": args["host"],
            "ssh_user": args["ssh_user"],
            "ssh_port": args["ssh_port"],
            "ssh_key_path": args["ssh_key_path"],
        }
        creds = resolve_vps_connection(overrides, fallback=marker_vps)
        provider = PROVIDERS_MAP[provider_enum](credentials=creds)

    try:
        deleted = destroy_session(provider, provider_enum.value, region)
    except Exception as exc:
        logger.debug("CLI destroy failed", exc_info=True)
        print(f"Destroy failed:\n{describe_pulumi_error(exc)}", file=sys.stderr)
        return 1

    if provider_enum == ProviderEnum.VPS:
        print("VPN tunnel removed (client disconnected, server configs cleaned).")
    else:
        print(f"Deleted VM. Count: {deleted}")
    return 0
