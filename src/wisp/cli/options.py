"""Reusable argument definitions and option helpers for CLI subparsers."""

import argparse
import logging

from wisp.cli.args import RuntimeArgs
from wisp.config.settings import get_default_provider
from wisp.providers import ProviderEnum
from wisp.utils.logger import logger

PROVIDER_CHOICES: list[str] = [
    provider.value for provider in ProviderEnum if provider != ProviderEnum.UNKNOWN
]


def add_provider(parser: argparse.ArgumentParser) -> None:
    """Add positional provider argument to the parser.

    Args:
        parser: Target argument parser.
    """
    parser.add_argument(
        "provider",
        nargs="?",
        type=str,
        choices=PROVIDER_CHOICES,
        default=get_default_provider(),
        help="Cloud provider to use (default %(default)s).",
    )


def add_log_level(parser: argparse.ArgumentParser) -> None:
    """Add --log-level console verbosity option to the parser.

    Args:
        parser: Target argument parser.
    """
    parser.add_argument(
        "--log-level",
        type=str.lower,
        default="info",
        choices=["debug", "info", "warning", "error"],
        help=(
            "Console log verbosity (default: %(default)s). The rotating "
            "file log always keeps DEBUG."
        ),
    )


def add_region(parser: argparse.ArgumentParser, *, help_text: str) -> None:
    """Add -r/--region option to the parser.

    Args:
        parser: Target argument parser.
        help_text: Help string specific to the command context.
    """
    parser.add_argument(
        "-r",
        "--region",
        type=str,
        default=None,
        help=help_text,
    )


def add_vps_options(parser: argparse.ArgumentParser, *, marker: bool) -> None:
    """Add VPS connection options to the parser.

    Args:
        parser: Target argument parser.
        marker: True if help strings should reference the deploy-time marker.
    """
    if not marker:
        parser.add_argument(
            "--host",
            type=str,
            default=None,
            metavar="HOST",
            help="VPS only: the server's public hostname or IP (overrides [vps].host).",
        )
        parser.add_argument(
            "--ssh-user",
            type=str,
            default=None,
            metavar="USER",
            help="VPS only: SSH user to connect as (overrides [vps].ssh_user).",
        )
        parser.add_argument(
            "--ssh-port",
            type=int,
            default=None,
            metavar="PORT",
            help="VPS only: SSH port (overrides [vps].ssh_port).",
        )
        parser.add_argument(
            "--ssh-key-path",
            type=str,
            default=None,
            metavar="PATH",
            help="VPS only: path to your SSH private key (overrides [vps].ssh_key_path).",
        )
    else:
        parser.add_argument(
            "--host",
            type=str,
            default=None,
            metavar="HOST",
            help=(
                "VPS only: the server's hostname/IP (overrides the deploy-time "
                "marker and [vps].host)."
            ),
        )
        parser.add_argument(
            "--ssh-user",
            type=str,
            default=None,
            metavar="USER",
            help="VPS only: SSH user (overrides the marker and [vps].ssh_user).",
        )
        parser.add_argument(
            "--ssh-port",
            type=int,
            default=None,
            metavar="PORT",
            help="VPS only: SSH port (overrides the marker and [vps].ssh_port).",
        )
        parser.add_argument(
            "--ssh-key-path",
            type=str,
            default=None,
            metavar="PATH",
            help=(
                "VPS only: SSH private key path (overrides the marker and "
                "[vps].ssh_key_path)."
            ),
        )


def apply_log_level(args: RuntimeArgs) -> None:
    """Apply console log level to the second logger handler.

    Args:
        args: Runtime arguments containing log_level.
    """
    level_name = (args["log_level"] or "info").upper()
    logger.handlers[1].setLevel(getattr(logging, level_name, logging.INFO))
