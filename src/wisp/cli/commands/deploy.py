"""Deploy CLI command handler."""

import argparse
import json
import sys
from typing import Any

from wisp.cli.args import RuntimeArgs
from wisp.cli.commands.common import require_provider, resolve_region
from wisp.cli.options import (
    add_log_level,
    add_provider,
    add_region,
    add_vps_options,
    apply_log_level,
)
from wisp.config.settings import load_wisp_config
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.services.deploy import TunnelConflictError, deploy_session
from wisp.services.providers import (
    create_provider,
    resolve_vps_connection,
    vps_payload,
)
from wisp.utils.logger import logger
from wisp.utils.pulumi import describe_pulumi_error


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the deploy command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    parser = subparsers.add_parser(
        "deploy",
        help="Deploy a VM",
    )
    add_provider(parser)
    add_log_level(parser)
    add_region(
        parser,
        help_text=(
            "Region where the VM will be deployed "
            "(defaults to the provider's configured region)."
        ),
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        metavar="MINUTES",
        help=(
            "Session TTL watchdog: run `wisp destroy` automatically after "
            "this many minutes, even if Wisp exits (default: the configured "
            "[general].max_session_ttl_minutes; 0 disables the watchdog)."
        ),
    )
    parser.add_argument(
        "--wireguard-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "UDP port for the WireGuard tunnel (default: the configured "
            "[general].wireguard_port; 0 picks a random port)."
        ),
    )
    add_vps_options(parser, marker=False)


def run(args: RuntimeArgs) -> int:
    """Execute the deploy command.

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

    config = load_wisp_config()
    if args["wireguard_port"] is not None:
        config.wireguard_port = max(0, int(args["wireguard_port"]))

    ttl_minutes = (
        args["ttl_minutes"]
        if args["ttl_minutes"] is not None
        else config.max_session_ttl_minutes
    )
    config.max_session_ttl_minutes = ttl_minutes

    vps_payload_data: dict[str, Any] | None = None
    if provider_enum == ProviderEnum.VPS:
        overrides = {
            "host": args["host"],
            "ssh_user": args["ssh_user"],
            "ssh_port": args["ssh_port"],
            "ssh_key_path": args["ssh_key_path"],
        }
        creds = resolve_vps_connection(overrides)
        provider = PROVIDERS_MAP[ProviderEnum.VPS](credentials=creds)
        vps_payload_data = vps_payload(creds)

    try:
        vm_result = deploy_session(
            provider,
            provider_enum.value,
            region,
            config,
            vps_payload=vps_payload_data,
        )
    except TunnelConflictError as exc:
        print(exc, file=sys.stderr)
        print(
            "No VM was deployed. Wisp only manages the WireGuard tunnel it created.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        logger.debug("CLI deploy failed", exc_info=True)
        print(f"Deployment failed:\n{describe_pulumi_error(exc)}", file=sys.stderr)
        return 1

    print(f"Deployed VM:\n{json.dumps(vm_result, indent=2)}")
    return 0
