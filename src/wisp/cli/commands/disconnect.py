"""Disconnect CLI command handler."""

import argparse
import sys

from wisp.cli.args import RuntimeArgs
from wisp.wireguard import disconnect_wireguard_client


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the disconnect command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    subparsers.add_parser(
        "disconnect",
        help="Take the local WireGuard client down (local panic switch)",
        description=(
            "Immediately bring the local WireGuard client interface down "
            "through the privileged daemon. Purely local: no cloud API calls, "
            "no Pulumi state changes, no provider credentials.\n\n"
            "Use this when a dead VM or an interrupted session left the "
            "interface up and it is capturing your default route."
        ),
        epilog=(
            "Example:\n"
            "  wisp disconnect\n\n"
            "This leaves the cloud resources running; use 'wisp destroy' to "
            "stop billing.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def run(args: RuntimeArgs) -> int:
    """Execute the disconnect command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    disconnect_response = disconnect_wireguard_client()
    if disconnect_response.ok:
        print("WireGuard client disconnected successfully.")
        return 0
    else:
        print(
            f"Failed to disconnect WireGuard client: {disconnect_response.message}",
            file=sys.stderr,
        )
        return 1
