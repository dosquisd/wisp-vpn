"""Doctor CLI command handler."""

import argparse

from wisp.cli.args import RuntimeArgs
from wisp.cli.options import add_log_level


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the doctor command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check this machine for missing prerequisites",
        description=(
            "Check this machine for missing prerequisites (daemon, WireGuard "
            "tools, Pulumi, credentials, directories, etc.)."
        ),
    )
    add_log_level(doctor_parser)


def run(args: RuntimeArgs) -> int:
    """Execute the doctor command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    from wisp.doctor import run_doctor_command

    return run_doctor_command()
