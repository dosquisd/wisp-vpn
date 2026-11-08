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
        help="Disconnect the local WireGuard client from the remote VM",
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
