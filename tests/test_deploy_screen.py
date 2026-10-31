"""Deploy screen: refuse to provision when the local tunnel name is taken.

`wg0` is the default name of every WireGuard setup, and Wisp will not overwrite
another one. The conflict has to be caught before provisioning starts, because
discovering it afterwards leaves a billable VM with nothing attached to it.
"""

from textual.widgets import Select, Switch

from wisp.cli.screens import DeployScreen, ProgressScreen
from wisp.cli.screens import deploy as deploy_module


async def _open_deploy(pilot, app) -> None:
    app.push_screen("deploy")
    await pilot.pause()
    await pilot.pause()


async def test_conflicting_tunnel_keeps_the_user_on_the_form(wisp_app, monkeypatch):
    monkeypatch.setattr(
        deploy_module,
        "tunnel_conflict",
        lambda: "WireGuard tunnel 'wg0' already exists and was not created by Wisp.",
    )

    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_deploy(pilot, wisp_app)
        screen = wisp_app.screen
        assert isinstance(screen, DeployScreen)

        screen.start_deployment()
        await pilot.pause()

        assert wisp_app.screen is screen
        assert not any(
            isinstance(entry, ProgressScreen) for entry in wisp_app.screen_stack
        )


async def test_free_tunnel_name_starts_the_deployment(wisp_app, monkeypatch):
    monkeypatch.setattr(deploy_module, "tunnel_conflict", lambda: None)
    pushed: list[object] = []

    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_deploy(pilot, wisp_app)
        screen = wisp_app.screen
        assert isinstance(screen, DeployScreen)

        # Capture the push instead of mounting it: the progress screen starts
        # the real provisioning thread on mount.
        monkeypatch.setattr(
            wisp_app, "push_screen", lambda target, *a, **kw: pushed.append(target)
        )
        screen.start_deployment()
        await pilot.pause()

    assert len(pushed) == 1
    assert isinstance(pushed[0], ProgressScreen)


async def test_the_form_still_offers_provider_and_ttl_controls(wisp_app):
    """Guard against the refusal path reading widgets that no longer exist."""
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_deploy(pilot, wisp_app)
        screen = wisp_app.screen

        assert isinstance(screen, DeployScreen)
        assert screen.query_one("#select-provider", Select) is not None
        assert screen.query_one("#switch-ttl", Switch) is not None
