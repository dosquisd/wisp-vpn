"""Session boot: shared guard arming and startup tunnel adoption.

Both behaviours are pure decision logic over the marker, the daemon reply and a
couple of side effects, so they are tested without a daemon, a cloud provider or
root. What matters is that adoption never takes over the wrong tunnel and never
extends a session's TTL by rewriting its marker.
"""

import time

import pytest

from wisp.cli import session_boot
from wisp.cli.app import WispApp
from wisp.cli.screens.main_menu import MainMenuScreen
from wisp.cli.screens.tunnel import (
    HEALTH_LIVE,
    HEALTH_LOCAL_DOWN,
    HEALTH_REMOTE_GONE,
    HEALTH_WAITING,
    TunnelScreen,
    health_state,
)
from wisp.daemon.protocol import Response
from wisp.session import ActiveSession


class _Recorder:
    """Collect calls made by the code under test."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def marker(self, *args, **kwargs) -> None:
        self.calls.append(("marker", args, kwargs))

    def watchdog(self, *args, **kwargs) -> None:
        self.calls.append(("watchdog", args, kwargs))


def _marker(pid: int = 999_999, **overrides) -> ActiveSession:
    """Build a session marker for the adoption tests.

    Args:
        pid (int): Owning process id. The default cannot be a live pid.
        **overrides: Fields to override on the marker.

    Returns:
        ActiveSession: The marker.
    """
    payload = {
        "provider": "aws",
        "region": "us-east-1",
        "pid": pid,
        "started_at": time.time() - 600,
        "ttl_minutes": 0,
        "expires_at": 0.0,
    }
    payload.update(overrides)
    return ActiveSession(payload)


def _stub_side_effects(monkeypatch) -> _Recorder:
    """Neutralize marker writes and the teardown, recording every call.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        _Recorder: Recorder holding the captured calls.
    """
    recorder = _Recorder()
    monkeypatch.setattr(session_boot, "write_active_session", recorder.marker)
    monkeypatch.setattr(session_boot, "schedule_ttl_watchdog", recorder.watchdog)
    monkeypatch.setattr(session_boot, "teardown_active_session", lambda *a, **k: None)
    return recorder


def test_arm_without_marker_or_watchdog_arms_only_the_guard(
    wisp_app, monkeypatch
) -> None:
    """Adoption arms the guard without rewriting or rescheduling anything."""
    recorder = _stub_side_effects(monkeypatch)

    session_boot.arm_session_guard(
        wisp_app,
        provider="aws",
        region="us-east-1",
        ttl_minutes=30,
        write_marker=False,
        schedule_watchdog=False,
    )

    assert recorder.calls == []
    assert wisp_app.state.session_guard.armed is True


def test_arm_with_marker_and_ttl_schedules_everything(wisp_app, monkeypatch) -> None:
    """A fresh deploy writes the marker and schedules the OS one-shot."""
    recorder = _stub_side_effects(monkeypatch)

    session_boot.arm_session_guard(
        wisp_app,
        provider="aws",
        region="us-east-1",
        ttl_minutes=30,
        write_marker=True,
        schedule_watchdog=True,
    )

    assert [call[0] for call in recorder.calls] == ["marker", "watchdog"]
    assert recorder.calls[0][2]["ttl_minutes"] == 30
    assert recorder.calls[1][1] == ("aws", "us-east-1", 30)


def test_arm_does_not_schedule_watchdog_without_ttl(wisp_app, monkeypatch) -> None:
    """No TTL means no OS one-shot, but the marker is still written."""
    recorder = _stub_side_effects(monkeypatch)

    session_boot.arm_session_guard(
        wisp_app,
        provider="aws",
        region="us-east-1",
        ttl_minutes=0,
    )

    assert [call[0] for call in recorder.calls] == ["marker"]


async def test_adoption_refuses_a_marker_owned_by_a_live_process(
    wisp_app, monkeypatch
) -> None:
    """Never take over a tunnel another running Wisp process still owns."""
    from wisp.cli import app as app_module

    monkeypatch.setattr(WispApp, "_offer_orphan_cleanup", lambda self: None)
    monkeypatch.setattr(app_module, "read_active_session", lambda: _marker(pid=1234))
    monkeypatch.setattr(app_module, "process_alive", lambda pid: True)

    async with wisp_app.run_test() as pilot:
        wisp_app._maybe_adopt_live_session()
        await pilot.pause()

        assert type(wisp_app.screen) is MainMenuScreen
        assert wisp_app.state.session_guard.armed is False


async def test_adoption_refuses_when_the_local_interface_is_down(
    wisp_app, monkeypatch
) -> None:
    """A marker with no live interface is an orphan, not a tunnel to adopt."""
    import wisp.wireguard as wireguard_module
    from wisp.cli import app as app_module

    monkeypatch.setattr(WispApp, "_offer_orphan_cleanup", lambda self: None)
    monkeypatch.setattr(app_module, "read_active_session", lambda: _marker())
    monkeypatch.setattr(app_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        wireguard_module, "status_wireguard_client", lambda: Response(ok=False)
    )

    async with wisp_app.run_test() as pilot:
        wisp_app._maybe_adopt_live_session()
        await pilot.pause()

        assert type(wisp_app.screen) is MainMenuScreen
        assert wisp_app.state.session_guard.armed is False


async def test_adoption_refuses_a_stale_handshake(wisp_app, monkeypatch) -> None:
    """A handshake older than the stale threshold is not a live tunnel."""
    import wisp.wireguard as wireguard_module
    from wisp.cli import app as app_module

    monkeypatch.setattr(WispApp, "_offer_orphan_cleanup", lambda self: None)
    monkeypatch.setattr(app_module, "read_active_session", lambda: _marker())
    monkeypatch.setattr(app_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        wireguard_module,
        "status_wireguard_client",
        lambda: Response(ok=True, handshake_age_seconds=600),
    )

    async with wisp_app.run_test() as pilot:
        wisp_app._maybe_adopt_live_session()
        await pilot.pause()

        assert type(wisp_app.screen) is MainMenuScreen
        assert wisp_app.state.session_guard.armed is False


async def test_adoption_takes_over_a_live_tunnel(wisp_app, monkeypatch) -> None:
    """A dead owner plus a fresh handshake is exactly what adoption is for."""
    import wisp.wireguard as wireguard_module
    from wisp.cli import app as app_module

    monkeypatch.setattr(WispApp, "_offer_orphan_cleanup", lambda self: None)
    monkeypatch.setattr(app_module, "read_active_session", lambda: _marker(pid=424_242))
    monkeypatch.setattr(app_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        wireguard_module,
        "status_wireguard_client",
        lambda: Response(ok=True, handshake_age_seconds=10),
    )
    recorder = _stub_side_effects(monkeypatch)

    async with wisp_app.run_test() as pilot:
        wisp_app._maybe_adopt_live_session()
        await pilot.pause()

        assert isinstance(wisp_app.screen, TunnelScreen)
        assert wisp_app.state.provider_name == "aws"
        assert wisp_app.state.selected_region == "us-east-1"
        assert wisp_app.state.session_guard.armed is True
        # Adoption must never reset the session's original start and expiry.
        assert recorder.calls == []


async def test_orphan_warning_is_skipped_when_the_session_can_be_adopted(
    wisp_app, monkeypatch
) -> None:
    """A live tunnel must not be advertised as an orphan to clean up.

    The user should land in the tunnel view, not on a warning telling them to
    destroy the very session that is about to be shown as running.
    """
    import wisp.wireguard as wireguard_module
    from wisp.cli import app as app_module

    marker = _marker(pid=424_242)
    monkeypatch.setattr(app_module, "find_orphaned_session", lambda: marker)
    monkeypatch.setattr(app_module, "read_active_session", lambda: marker)
    monkeypatch.setattr(app_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        wireguard_module,
        "status_wireguard_client",
        lambda: Response(ok=True, handshake_age_seconds=10),
    )
    notifications: list[str] = []
    monkeypatch.setattr(
        WispApp,
        "notify",
        lambda self, message, **kwargs: notifications.append(message),
    )

    async with wisp_app.run_test():
        wisp_app._offer_orphan_cleanup()

    assert notifications == []


async def test_orphan_warning_becomes_a_state_unknown_notice_without_handshake_age(
    wisp_app, monkeypatch
) -> None:
    """An interface with no reported handshake age must never invite a destroy.

    That is the signature of a daemon too old to report the field: the tunnel
    can be perfectly alive. Offering "clean up the orphan" here would push the
    user to delete a working session.
    """
    import wisp.wireguard as wireguard_module
    from wisp.cli import app as app_module

    marker = _marker(pid=424_242)
    monkeypatch.setattr(app_module, "find_orphaned_session", lambda: marker)
    monkeypatch.setattr(app_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        wireguard_module,
        "status_wireguard_client",
        lambda: Response(ok=True, handshake_age_seconds=None),
    )
    notifications: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        WispApp,
        "notify",
        lambda self, message, **kwargs: notifications.append((message, kwargs)),
    )

    async with wisp_app.run_test():
        wisp_app._offer_orphan_cleanup()

    assert len(notifications) == 1
    message, kwargs = notifications[0]
    assert kwargs["title"] == "Session state unknown"
    assert "daemon install" in message
    assert "Orphaned" not in message


async def test_orphan_warning_still_fires_when_the_tunnel_is_gone(
    wisp_app, monkeypatch
) -> None:
    """Without a live tunnel the warning is the whole point: resources may bill."""
    import wisp.wireguard as wireguard_module
    from wisp.cli import app as app_module

    marker = _marker(pid=424_242)
    monkeypatch.setattr(app_module, "find_orphaned_session", lambda: marker)
    monkeypatch.setattr(app_module, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        wireguard_module, "status_wireguard_client", lambda: Response(ok=False)
    )
    notifications: list[str] = []
    monkeypatch.setattr(
        WispApp,
        "notify",
        lambda self, message, **kwargs: notifications.append(message),
    )

    async with wisp_app.run_test():
        wisp_app._offer_orphan_cleanup()

    assert len(notifications) == 1
    assert "Orphaned tunnel found" in notifications[0]


def test_health_state_maps_every_evidence_case() -> None:
    """Only an older-than-stale handshake may declare the remote end gone."""
    assert health_state(Response(ok=False), elapsed=0) == HEALTH_LOCAL_DOWN
    assert health_state(Response(ok=True), elapsed=10) == HEALTH_WAITING
    assert health_state(Response(ok=True), elapsed=100_000) == HEALTH_REMOTE_GONE
    assert (
        health_state(Response(ok=True, handshake_age_seconds=600), elapsed=0)
        == HEALTH_REMOTE_GONE
    )
    assert (
        health_state(Response(ok=True, handshake_age_seconds=10), elapsed=0)
        == HEALTH_LIVE
    )


def test_read_endpoint_handles_missing_and_present_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adopted view reads the endpoint from the config, or admits it is unknown."""
    from wisp.cli.screens import tunnel as tunnel_module

    missing = tmp_path / "wg0.conf"
    monkeypatch.setattr(tunnel_module, "WIREGUARD_CLIENT_CONF_PATH", missing)
    assert tunnel_module._read_endpoint() is None

    missing.write_text("[Peer]\nPublicKey = abc\n", encoding="utf-8")
    assert tunnel_module._read_endpoint() is None

    missing.write_text(
        "[Peer]\nPublicKey = abc\nEndpoint = 203.0.113.7:51820\n",
        encoding="utf-8",
    )
    assert tunnel_module._read_endpoint() == "203.0.113.7:51820"
