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
        help="List the regions the provider offers",
        description=(
            "Query the provider for the regions it currently offers and "
            "print them as a JSON array. Uses the provider's own API: boto3 "
            "for AWS, the Identity API for OCI, and the Compute SDK for GCP. "
            "The 'vps' provider has no regions and returns a placeholder.\n\n"
            "This only reads the provider's region catalog; it needs working "
            "credentials but deploys nothing."
        ),
        epilog=(
            "Examples:\n  wisp regions aws\n  wisp regions oci\n  wisp regions gcp\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
