"""Daemon CLI command handler."""

import argparse

from wisp.cli.args import RuntimeArgs
from wisp.cli.options import add_log_level, apply_log_level


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the daemon command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Install or remove the privileged local daemon",
    )
    add_log_level(daemon_parser)
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command", required=True
    )
    daemon_subparsers.add_parser(
        "install",
        help="Install (or refresh) the privileged local daemon",
    )
    daemon_subparsers.add_parser(
        "uninstall",
        help="Remove the privileged local daemon",
    )


def run(args: RuntimeArgs) -> int:
    """Execute the daemon command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    apply_log_level(args)
    # Lazy import: importing wisp.daemon.install at module level would set
    # WISP_DAEMON=1 in this process (wisp/daemon/__init__.py) and flip the
    # CLI logger to the daemon's stderr-only DEBUG sink for every command.
    from wisp.daemon.install import install_daemon, uninstall_daemon

    if args["daemon_command"] == "install":
        return install_daemon()
    return uninstall_daemon()
