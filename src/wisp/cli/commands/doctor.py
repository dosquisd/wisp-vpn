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
            "Run read-only diagnostic checks for everything Wisp depends on: "
            "the daemon, WireGuard tools, Pulumi, per-provider credentials, "
            "and runtime directories. Every check prints PASS, WARN, SKIP, or "
            "FAIL with an actionable hint.\n\n"
            "Never modifies the machine. Exits 1 only on FAIL; WARN and SKIP "
            "exit 0."
        ),
        epilog="Example:\n  wisp doctor\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
