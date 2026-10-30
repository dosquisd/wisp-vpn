"""Tests for watchdog config generation and persistent keepalive insertion."""

from pathlib import Path

from wisp.wireguard.remote_server import (
    build_watchdog_config,
    ensure_persistent_keepalive,
)

SAMPLE_CLIENT_CONFIG_WITH_PEER = """[Interface]
PrivateKey = aaaa=
Address = 10.66.66.2/32
DNS = 1.1.1.1

[Peer]
PublicKey = bbbb=
AllowedIPs = 0.0.0.0/0
Endpoint = 198.51.100.1:51820
"""

SAMPLE_CLIENT_CONFIG_NO_PEER = """[Interface]
PrivateKey = aaaa=
Address = 10.66.66.2/32
DNS = 1.1.1.1
"""


def test_build_watchdog_config_cloud_mode_with_ttl() -> None:
    """Watchdog config in cloud mode with positive TTL computes deadline."""
    config = build_watchdog_config(
        nic="wg0",
        mode="cloud",
        ttl_minutes=60,
        idle_seconds=900,
        server_started_epoch=1700000000,
    )
    expected_deadline = 1700000000 + 60 * 60
    assert 'NIC="wg0"' in config
    assert 'MODE="cloud"' in config
    assert "IDLE_SECONDS=900" in config
    assert f"DEADLINE_EPOCH={expected_deadline}" in config
    assert "SESSION_STARTED_EPOCH=1700000000" in config


def test_build_watchdog_config_vps_mode_zero_ttl() -> None:
    """Watchdog config in vps mode with 0 TTL sets deadline to 0."""
    config = build_watchdog_config(
        nic="wisp-abc12",
        mode="vps",
        ttl_minutes=0,
        idle_seconds=300,
        server_started_epoch=1700001000,
    )
    assert 'NIC="wisp-abc12"' in config
    assert 'MODE="vps"' in config
    assert "IDLE_SECONDS=300" in config
    assert "DEADLINE_EPOCH=0" in config
    assert "SESSION_STARTED_EPOCH=1700001000" in config


def test_build_watchdog_config_negative_ttl_sets_deadline_zero() -> None:
    """A negative TTL must produce DEADLINE_EPOCH=0."""
    config = build_watchdog_config(
        nic="wg0",
        mode="cloud",
        ttl_minutes=-10,
        idle_seconds=600,
        server_started_epoch=1700000000,
    )
    assert "DEADLINE_EPOCH=0" in config


def test_ensure_persistent_keepalive_adds_line_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Adding keepalive must append to [Peer] and avoid duplication on second call."""
    conf_file = tmp_path / "client.conf"
    conf_file.write_text(SAMPLE_CLIENT_CONFIG_WITH_PEER, encoding="utf-8")

    # First call adds PersistentKeepalive = 25
    assert ensure_persistent_keepalive(conf_file) is True
    content = conf_file.read_text(encoding="utf-8")
    assert "PersistentKeepalive = 25" in content

    # Second call is a clean no-op
    assert ensure_persistent_keepalive(conf_file) is False
    content_second = conf_file.read_text(encoding="utf-8")
    assert content_second == content
    assert content_second.count("PersistentKeepalive") == 1


def test_ensure_persistent_keepalive_missing_peer_section(tmp_path: Path) -> None:
    """Configs without [Peer] section must not be modified and return False."""
    conf_file = tmp_path / "no_peer.conf"
    conf_file.write_text(SAMPLE_CLIENT_CONFIG_NO_PEER, encoding="utf-8")

    assert ensure_persistent_keepalive(conf_file) is False
    assert "PersistentKeepalive" not in conf_file.read_text(encoding="utf-8")


def test_ensure_persistent_keepalive_nonexistent_file(tmp_path: Path) -> None:
    """Non-existent files must return False without raising."""
    conf_file = tmp_path / "absent.conf"
    assert ensure_persistent_keepalive(conf_file) is False
