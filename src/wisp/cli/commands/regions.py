"""Regions CLI command handler."""

import argparse
import json

from wisp.cli.args import RuntimeArgs
from wisp.cli.commands.common import require_provider
from wisp.cli.options import add_provider, apply_log_level
from wisp.services.providers import create_provider
from wisp.utils.logger import logger


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the regions command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    parser = subparsers.add_parser(
        "regions",
        help="List available regions",
    )
    add_provider(parser)


def run(args: RuntimeArgs) -> int:
    """Execute the regions command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    apply_log_level(args)
    provider_enum = require_provider(args)
    provider = create_provider(provider_enum)

    logger.info(f"Fetching available regions for provider: {provider_enum.value}")
    regions = provider.get_available_regions()
    print(f"Available regions:\n{json.dumps(regions, indent=2)}")
    return 0
