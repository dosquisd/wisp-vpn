"""Layout regressions: nothing the user must see may scroll out of reach.

Textual's default cards use `max-height: 100%` with `overflow-y: auto`, so any
card taller than the terminal grows a scrollbar. The deploy screen is the one
whose bottom (the Start/Cancel buttons) must stay reachable; on a 24-row
terminal a scrollbar elsewhere is tolerable, but the progress and menu cards
must fit the smallest realistic terminal.
"""


def _assert_vertically_centered(widget) -> None:
    parent_region = widget.parent.region
    parent_center = parent_region.y + parent_region.height / 2
    widget_center = widget.region.y + widget.region.height / 2
    assert abs(widget_center - parent_center) <= 1, (
        f"widget is not vertically centered: "
        f"widget={widget_center:.1f} parent={parent_center:.1f}"
    )


async def test_deploy_card_fits_a_24_row_terminal(wisp_app):
    """The deploy card must not overflow, so the buttons are never clipped."""
    async with wisp_app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        wisp_app.push_screen("deploy")
        await pilot.pause()

        card = wisp_app.screen.query_one(".card")
        assert card.virtual_size.height <= card.container_size.height, (
            "deploy card overflows: "
            f"virtual={card.virtual_size.height} container={card.container_size.height}"
        )


async def test_deploy_card_is_content_sized_and_centered(wisp_app):
    """The deploy card should not stretch through the available terminal height."""
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        wisp_app.push_screen("deploy")
        await pilot.pause()

        card = wisp_app.screen.query_one("#deploy-card")
        assert card.region.height < card.parent.region.height
        _assert_vertically_centered(card)


async def test_deploy_card_height_is_stable_when_selects_focus(wisp_app, monkeypatch):
    """Focusing either Select must not resize the deployment card."""
    from wisp.tui.screens.deploy import DeployScreen

    monkeypatch.setattr(DeployScreen, "fetch_live_regions", lambda self: None)
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        wisp_app.push_screen("deploy")
        await pilot.pause()

        card = wisp_app.screen.query_one("#deploy-card")
        initial_height = card.region.height

        await pilot.click("#select-provider")
        await pilot.pause()
        assert card.region.height == initial_height

        await pilot.click("#select-region")
        await pilot.pause()
        assert card.region.height == initial_height


async def test_confirm_discard_modal_is_content_sized_and_centered(wisp_app):
    """The shared modal card should stay compact and centered."""
    from wisp.tui.screens.modal import ConfirmDiscardScreen

    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        wisp_app.push_screen(ConfirmDiscardScreen())
        await pilot.pause()

        card = wisp_app.screen.query_one(".modal-card")
        assert card.region.height < card.parent.region.height
        _assert_vertically_centered(card)


async def test_progress_card_fits_even_with_error(wisp_app):
    """The progress card must not scroll, including after a failed deploy."""
    async with wisp_app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        from wisp.tui.screens.progress import ProgressScreen

        screen = ProgressScreen()
        wisp_app.push_screen(screen)
        await pilot.pause()
        ProgressScreen.run_deployment_worker = lambda self: None  # type: ignore[method-assign]
        screen._handle_error("Falló el despliegue: credenciales inválidas en us-east-1")
        await pilot.pause()

        card = screen.query_one(".card")
        assert card.virtual_size.height <= card.container_size.height, (
            f"progress card overflows: virtual={card.virtual_size.height}"
        )


async def test_progress_bar_is_centered(wisp_app):
    """The ProgressBar must sit in the middle of the card content, not hug the left."""
    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        from wisp.tui.screens.progress import ProgressScreen

        screen = ProgressScreen()
        wisp_app.push_screen(screen)
        await pilot.pause()

        card = screen.query_one(".card")
        bar = screen.query_one("#progress-bar")
        card_left = card.content_region.x
        card_width = card.content_region.width
        bar_center = bar.region.x + bar.region.width / 2
        expected = card_left + card_width / 2
        assert abs(bar_center - expected) <= 1, (
            f"progress bar off-centre by {bar_center - expected:+.1f} cols"
        )

        visual_left = bar.query_one("#bar").region.x
        visual_right = bar.query_one("#percentage").region.right
        visual_center = (visual_left + visual_right) / 2
        assert abs(visual_center - expected) <= 1, (
            f"progress contents off-centre by {visual_center - expected:+.1f} cols"
        )


async def test_shutdown_progress_bar_is_centered_and_fits(wisp_app):
    """The shutdown ProgressBar should stay centered within the card content."""
    from wisp.tui.screens.shutdown import ShutdownScreen

    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        ShutdownScreen.start_teardown = lambda self: None  # type: ignore[method-assign]
        screen = ShutdownScreen(exit_after=False)
        wisp_app.push_screen(screen)
        await pilot.pause()

        card = screen.query_one(".card")
        bar = screen.query_one("#shutdown-progress")
        card_left = card.content_region.x
        card_width = card.content_region.width
        bar_center = bar.region.x + bar.region.width / 2
        expected = card_left + card_width / 2
        assert abs(bar_center - expected) <= 1, (
            f"shutdown progress bar off-centre by {bar_center - expected:+.1f} cols"
        )
        assert bar.region.width < card_width


async def test_shutdown_returns_to_main_menu(wisp_app):
    """Completed teardown should return to the menu instead of exiting the app."""
    from wisp.tui.screens import MainMenuScreen
    from wisp.tui.screens.shutdown import ShutdownScreen

    async with wisp_app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        ShutdownScreen.start_teardown = lambda self: None  # type: ignore[method-assign]
        screen = ShutdownScreen()
        wisp_app.push_screen(screen)
        await pilot.pause()

        screen._exit_now()
        await pilot.pause()

        assert isinstance(wisp_app.screen, MainMenuScreen)
