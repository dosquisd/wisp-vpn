"""Connect CLI command handler."""

import argparse
import sys

from wisp.cli.args import RuntimeArgs
from wisp.config.constants import WIREGUARD_CLIENT_CONF_PATH
from wisp.wireguard import connect_wireguard_client


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the connect command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    subparsers.add_parser(
        "connect",
        help="Connect the local WireGuard client to the remote VM",
    )


def run(args: RuntimeArgs) -> int:
    """Execute the connect command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    if not WIREGUARD_CLIENT_CONF_PATH.exists():
        print(
            f"WireGuard client config not found at {WIREGUARD_CLIENT_CONF_PATH}. "
            "Please deploy a VM first.",
            file=sys.stderr,
        )
        return 1

    content = WIREGUARD_CLIENT_CONF_PATH.read_text()
    connect_response = connect_wireguard_client(content)
    if connect_response.ok:
        print("WireGuard client connected successfully.")
        return 0
    else:
        print(
            f"Failed to connect WireGuard client: {connect_response.message}",
            file=sys.stderr,
        )
        return 1
