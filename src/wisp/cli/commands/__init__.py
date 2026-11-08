"""Subcommand implementations for the Wisp CLI."""

from types import ModuleType

from wisp.cli.args import CommandEnum
from wisp.cli.commands import (
    connect,
    daemon,
    deploy,
    destroy,
    disconnect,
    doctor,
    gc,
    regions,
)

COMMANDS: dict[CommandEnum, ModuleType] = {
    CommandEnum.CONNECT: connect,
    CommandEnum.DAEMON: daemon,
    CommandEnum.DEPLOY: deploy,
    CommandEnum.DESTROY: destroy,
    CommandEnum.DISCONNECT: disconnect,
    CommandEnum.DOCTOR: doctor,
    CommandEnum.GC: gc,
    CommandEnum.REGIONS: regions,
}

__all__ = ["COMMANDS"]
