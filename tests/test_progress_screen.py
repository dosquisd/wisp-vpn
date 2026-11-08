"""Progress/result screen: its destroy path must ask via the same
``ConfirmDestroyScreen`` modal as the tunnel view, and route the teardown
through ``request_shutdown`` (the armed session guard) — never straight to
``delete_vm``. The deploy worker is stubbed so mounting the screen is safe.
"""

from wisp.tui.screens.modal import ConfirmDestroyScreen
from wisp.tui.screens.progress import ProgressScreen


def _stub_deploy(monkeypatch) -> None:
    monkeypatch.setattr(ProgressScreen, "run_deployment_worker", lambda self: None)


def _stub_shutdown(monkeypatch):
    calls = []

    def helper(app, reason) -> None:
        calls.append((app, reason))

    monkeypatch.setattr(
        "wisp.tui.screens.shutdown.request_shutdown",
        helper,
    )
    monkeypatch.setattr(
        "wisp.tui.screens.progress.request_shutdown",
        helper,
    )
    return calls


async def _open_progress(wisp_app, pilot) -> ProgressScreen:
    wisp_app.push_screen(ProgressScreen())
    await pilot.pause()
    await pilot.pause()
    assert type(wisp_app.screen) is ProgressScreen
    return wisp_app.screen


async def test_results_destroy_asks_first(wisp_app, monkeypatch):
    _stub_deploy(monkeypatch)
    calls = _stub_shutdown(monkeypatch)
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        screen = await _open_progress(wisp_app, pilot)

        screen._confirm_then_destroy()
        await pilot.pause()

        assert isinstance(wisp_app.screen, ConfirmDestroyScreen)
        assert not calls

        await pilot.click("#btn-destroy-no")
        await pilot.pause()
        assert type(wisp_app.screen) is ProgressScreen
        assert not calls


async def test_results_destroy_confirmed_routes_to_shutdown(wisp_app, monkeypatch):
    _stub_deploy(monkeypatch)
    calls = _stub_shutdown(monkeypatch)
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        screen = await _open_progress(wisp_app, pilot)

        screen._confirm_then_destroy()
        await pilot.pause()
        await pilot.click("#btn-destroy-yes")
        await pilot.pause()

        assert len(calls) == 1
        assert "progress screen" in calls[0][1]


async def test_results_destroy_skips_modal_when_disabled(wisp_app, monkeypatch):
    _stub_deploy(monkeypatch)
    calls = _stub_shutdown(monkeypatch)
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_progress(wisp_app, pilot)
        wisp_app.state.config.confirm_destroy = False

        wisp_app.screen._confirm_then_destroy()
        await pilot.pause()

        assert len(calls) == 1
        assert not isinstance(wisp_app.screen, ConfirmDestroyScreen)
