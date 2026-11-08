"""Tunnel view: destroy confirmation (``[general] confirm_destroy``).

The only way out of the tunnel view is destroying the VPN, which is
irreversible, so it must have the described guard: ask by default, skip when
disabled, and persist "no volver a preguntar". ``request_shutdown`` and
``save_session_config`` are stubbed so nothing touches the real daemon or disk.
"""

from textual.widgets import Switch

from wisp.tui.screens.modal import ConfirmDestroyScreen
from wisp.tui.screens.tunnel import TunnelScreen


async def _open_tunnel(pilot, app) -> None:
    app.push_screen(TunnelScreen())
    await pilot.pause()
    await pilot.pause()
    assert type(app.screen) is TunnelScreen


def _stub_shutdown(monkeypatch):
    """Capture request_shutdown calls from both call sites.

    ``confirmed_destroy`` (the confirmed path) and the direct escape-hatch in
    the tunnel screen bind the same function under two module names, so both
    are patched to collect into one list.
    """
    calls = []

    def helper(app, reason) -> None:
        calls.append((app, reason))

    monkeypatch.setattr(
        "wisp.tui.screens.shutdown.request_shutdown",
        helper,
    )
    monkeypatch.setattr(
        "wisp.tui.screens.tunnel.request_shutdown",
        helper,
    )
    return calls


def _stub_save(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "wisp.tui.screens.shutdown.save_session_config",
        lambda *a, **k: calls.append(a),
    )
    return calls


async def test_destroy_asks_by_default(wisp_app):
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)

        await pilot.press("d")
        await pilot.pause()

        assert isinstance(wisp_app.screen, ConfirmDestroyScreen)

        await pilot.click("#btn-destroy-no")
        await pilot.pause()
        assert type(wisp_app.screen) is TunnelScreen


async def test_destroy_cancelled_by_escape(wisp_app):
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(wisp_app.screen, ConfirmDestroyScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert type(wisp_app.screen) is TunnelScreen


async def test_destroy_confirmed_triggers_shutdown(wisp_app, monkeypatch):
    calls = _stub_shutdown(monkeypatch)
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)

        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#btn-destroy-yes")
        await pilot.pause()

        assert len(calls) == 1
        assert "tunnel destroyed" in calls[0][1]


async def test_destroy_no_ask_persists_and_proceeds(wisp_app, monkeypatch):
    _stub_shutdown(monkeypatch)
    saves = _stub_save(monkeypatch)
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)
        assert wisp_app.state.config.confirm_destroy is True

        await pilot.press("d")
        await pilot.pause()
        modal = wisp_app.screen
        assert isinstance(modal, ConfirmDestroyScreen)
        modal.query_one("#switch-no-ask-destroy", Switch).value = True
        await pilot.pause()
        await pilot.click("#btn-destroy-yes")
        await pilot.pause()

        assert wisp_app.state.config.confirm_destroy is False
        assert len(saves) == 1


async def test_destroy_skips_modal_when_disabled(wisp_app, monkeypatch):
    calls = _stub_shutdown(monkeypatch)
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)
        wisp_app.state.config.confirm_destroy = False

        await pilot.press("d")
        await pilot.pause()

        assert len(calls) == 1
        assert not isinstance(wisp_app.screen, ConfirmDestroyScreen)


def _arm_guard(app) -> None:
    app.state.session_guard.arm(lambda reason: None)


def _stub_real_request_shutdown(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "wisp.tui.screens.shutdown.request_shutdown",
        lambda app, reason: calls.append((app, reason)),
    )
    return calls


async def test_quit_asks_when_tunnel_live(wisp_app):
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)
        _arm_guard(wisp_app)

        await pilot.press("q")
        await pilot.pause()

        assert isinstance(wisp_app.screen, ConfirmDestroyScreen)

        await pilot.click("#btn-destroy-no")
        await pilot.pause()
        assert type(wisp_app.screen) is TunnelScreen
        assert wisp_app.state.session_guard.armed


async def test_quit_confirmed_starts_teardown_with_reason(wisp_app, monkeypatch):
    calls = _stub_real_request_shutdown(monkeypatch)
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)
        _arm_guard(wisp_app)

        await pilot.press("q")
        await pilot.pause()
        await pilot.click("#btn-destroy-yes")
        await pilot.pause()

        assert len(calls) == 1
        assert "on quit" in calls[0][1]


async def test_quit_exits_without_teardown_when_nothing_live(wisp_app):
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)
        assert not wisp_app.state.session_guard.armed

        await pilot.press("q")
        await pilot.pause()

        assert not isinstance(wisp_app.screen, ConfirmDestroyScreen)
        assert wisp_app.return_value is None


async def test_quit_does_not_stack_a_second_modal(wisp_app):
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await _open_tunnel(pilot, wisp_app)
        _arm_guard(wisp_app)

        await pilot.press("q")
        await pilot.pause()
        assert isinstance(wisp_app.screen, ConfirmDestroyScreen)

        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(wisp_app.screen, ConfirmDestroyScreen)

        await pilot.click("#btn-destroy-no")
        await pilot.pause()
        assert type(wisp_app.screen) is TunnelScreen
