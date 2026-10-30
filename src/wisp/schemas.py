"""Typed data structures shared across the application."""

from pathlib import Path
from typing import TypedDict


class InventoryContext(TypedDict):
    """Configuration variables consumed during remote server setup.

    Populated during deploy from provider outputs and the active
    :class:`~wisp.config.settings.WispConfig`, then consumed by
    :func:`~wisp.wireguard.remote_server.configure_remote_server` to configure
    the remote WireGuard server, download client credentials, and install the
    in-VM session watchdog.
    """

    ssh_user: str
    instance_ip: str
    ssh_key_file: str | Path
    wireguard_port: int | str
    allowed_ips: str
    wireguard_public_ip: str
    wireguard_interface: str
    wireguard_ipv4: str
    wireguard_ipv6: str
    wireguard_dns1: str
    wireguard_dns2: str
    wireguard_client_name: str
    wireguard_client_ipv4: str
    wireguard_client_ipv6: str
    wireguard_skip_client: str
    watchdog_mode: str
    watchdog_ttl_minutes: int
    watchdog_idle_seconds: int
