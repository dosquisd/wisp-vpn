"""Argument parsing and top-level CLI dispatch."""

import argparse

from wisp.cli.args import CommandEnum, RuntimeArgs
from wisp.cli.commands import COMMANDS
from wisp.config.settings import ensure_user_config
from wisp.providers import ProviderEnum


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all registered subcommands.

    Returns:
        argparse.ArgumentParser: Configured argument parser.
    """
    parser = argparse.ArgumentParser(description="Wisp CLI")
    subparsers = parser.add_subparsers(dest="command")

    for command in COMMANDS.values():
        command.register(subparsers)

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
