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
        description=(
            "Manage the privileged local daemon that performs WireGuard "
            "operations on behalf of the unprivileged frontend. Requires "
            "root/sudo on Linux and an elevated terminal on Windows.\n\n"
            "Re-running 'install' refreshes an existing installation: the "
            "daemon runs from a root-owned venv, so daemon-side code changes "
            "only take effect after reinstalling and restarting the service."
        ),
        epilog=(
            "Examples:\n  sudo wisp daemon install\n  sudo wisp daemon uninstall\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_log_level(daemon_parser)
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command", required=True
    )
    daemon_subparsers.add_parser(
        "install",
        help="Install (or refresh) the privileged local daemon",
        description=(
            "Build the root-owned daemon venv and register the service: "
            "systemd socket activation on Linux (and add your user to the "
            "'wisp' group when run under sudo — log out and back in for it to "
            "apply), or a native auto-start service on Windows."
        ),
        epilog=(
            "Examples:\n"
            '  sudo env PYTHONDONTWRITEBYTECODE=1 "$(command -v wisp)" '
            "daemon install\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    daemon_subparsers.add_parser(
        "uninstall",
        help="Remove the privileged local daemon",
        description=(
            "Disable and delete the service and its root-owned runtime. The "
            "'wisp' group is kept; its lifecycle belongs to the bootstrap "
            "installer, which records whether it created the group."
        ),
        epilog=(
            "Examples:\n"
            '  sudo env PYTHONDONTWRITEBYTECODE=1 "$(command -v wisp)" '
            "daemon uninstall\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
