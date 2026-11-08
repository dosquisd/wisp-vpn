"""Command definitions and runtime argument schemas for the Wisp CLI."""

import enum
from typing import TypedDict

from wisp.providers import ProviderEnum


class CommandEnum(enum.StrEnum):
    """Top-level commands accepted by the CLI."""

    TUI = "tui"
    DEPLOY = "deploy"
    DESTROY = "destroy"
    REGIONS = "regions"
    GC = "gc"
    DAEMON = "daemon"
    DISCONNECT = "disconnect"  # panic option: disconnect the local WireGuard client without destroying the remote VM
    CONNECT = "connect"
    DOCTOR = "doctor"


class RuntimeArgs(TypedDict):
    """Normalized runtime arguments produced by :func:`parse_args`."""

    command: CommandEnum
    provider: ProviderEnum | None
    region: str | None
    gc_destroy: bool
    gc_refresh: bool
    daemon_command: str | None
    ttl_minutes: int | None
    wireguard_port: int | None
    host: str | None
    ssh_user: str | None
    ssh_port: int | None
    ssh_key_path: str | None
    log_level: str | None
