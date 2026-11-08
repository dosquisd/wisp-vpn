"""Argument parsing and top-level CLI dispatch."""

import argparse
import sys

from wisp.cli.args import CommandEnum, RuntimeArgs
from wisp.cli.commands import COMMANDS
from wisp.config.settings import ensure_user_config
from wisp.providers import ProviderEnum
from wisp.version import get_version


def _print_version() -> None:
    """Print the Wisp version line reported by ``--version``."""
    print(f"wisp {get_version()}")


class _VersionAction(argparse.Action):
    """Print the version and exit, resolving it only when the flag is used."""

    def __init__(self, option_strings: list[str], dest: str, **kwargs) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values,
        option_string: str | None = None,
    ) -> None:
        _print_version()
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all registered subcommands.

    Returns:
        argparse.ArgumentParser: Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="wisp",
        description=(
            "Wisp provisions ephemeral WireGuard VPNs on your own cloud.\n"
            "Run without a subcommand to launch the interactive TUI."
        ),
        epilog=(
            "Run 'wisp <command> --help' for command details and examples.\n"
            "Configuration lives in wisp.toml (see wisp.toml.example)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", title="commands")

    for command in COMMANDS.values():
        command.register(subparsers)

    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="show the installed version and exit",
    )

    return parser


def parse_args(argv: list[str] | None = None) -> RuntimeArgs:
    """Parse CLI arguments into normalized :class:`RuntimeArgs`.

    Args:
        argv: Argument list to parse; ``None`` parses ``sys.argv[1:]``.

    Returns:
        RuntimeArgs: Normalized runtime arguments.
    """
    parser = build_parser()
    args = parser.parse_args(argv).__dict__

    return RuntimeArgs(
        command=(
            CommandEnum(args["command"].lower())
            if args.get("command") is not None
            else CommandEnum.TUI
        ),
        provider=(
            ProviderEnum(args["provider"].lower())
            if args.get("provider") is not None
            else None
        ),
        region=args["region"].lower() if args.get("region") is not None else None,
        gc_destroy=bool(args.get("destroy", False)),
        gc_refresh=bool(args.get("refresh", False)),
        daemon_command=args.get("daemon_command"),
        ttl_minutes=args.get("ttl"),
        wireguard_port=args.get("wireguard_port"),
        host=args.get("host"),
        ssh_user=args.get("ssh_user"),
        ssh_port=args.get("ssh_port"),
        ssh_key_path=args.get("ssh_key_path"),
        log_level=args.get("log_level"),
    )


def main(argv: list[str] | None = None) -> int:
    """Run Wisp: launch the TUI or execute a non-interactive CLI command.

    Args:
        argv: Optional argument list; defaults to None (which parses sys.argv[1:]).

    Returns:
        int: Process exit code.
    """
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    # --version wins wherever it appears, so `wisp deploy oci --version`
    # reports the version instead of deploying anything.
    if "--version" in raw_argv:
        _print_version()
        return 0

    args = parse_args(argv)
    command = args["command"]

    if command == CommandEnum.DOCTOR:
        return COMMANDS[CommandEnum.DOCTOR].run(args)

    ensure_user_config()

    if command == CommandEnum.TUI:
        from wisp.tui.app import WispApp

        app = WispApp()
        app.run()
        return 0

    return COMMANDS[command].run(args)
