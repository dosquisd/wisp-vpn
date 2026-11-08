"""Config screen: unsaved-changes tracking and the discard guard.

Editing a field must be visible as "unsaved", leaving clean must pop straight
away, and leaving (or resetting) dirty must ask before discarding anything.
`save_session_config` is stubbed so saving never touches the developer's real
`wisp.toml`.
"""

from textual.widgets import Button, Input, Static

from wisp.tui.screens import ConfigScreen, MainMenuScreen
from wisp.tui.screens.modal import ConfirmActionScreen, ConfirmDiscardScreen


async def _open_config(pilot, app) -> None:
    app.push_screen("config")
    await pilot.pause()
    await pilot.pause()


async def _edit_port(screen, pilot, value: str = "51999") -> None:
    port = screen.query_one("#input-wireguard-port", Input)
    port.value = value
    await pilot.pause()


async def test_editing_a_field_marks_the_form_dirty(wisp_app, monkeypatch):
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        screen = wisp_app.screen
        assert isinstance(screen, ConfigScreen)
        assert screen._is_dirty() is False

        await _edit_port(screen, pilot)

        assert screen._is_dirty() is True
        marker = screen.query_one("#dirty-marker", Static)
        assert "Unsaved changes" in str(marker.render()).strip()


async def test_clean_form_pops_straight_back(wisp_app):
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(wisp_app.screen, MainMenuScreen)


async def test_dirty_form_asks_before_leaving(wisp_app):
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        await _edit_port(wisp_app.screen, pilot)

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(wisp_app.screen, ConfirmDiscardScreen)

        # Cancel keeps both screens: the modal closes, config stays, edits stay.
        await pilot.click("#btn-discard-cancel")
        await pilot.pause()
        assert isinstance(wisp_app.screen, ConfigScreen)
        assert wisp_app.screen._is_dirty() is True


async def test_discard_from_back_reverts_and_pops(wisp_app):
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        original_port = wisp_app.state.config.wireguard_port
        await _edit_port(wisp_app.screen, pilot)

        await pilot.press("escape")
        await pilot.pause()
        await pilot.click("#btn-discard-drop")
        await pilot.pause()

        assert isinstance(wisp_app.screen, MainMenuScreen)
        assert wisp_app.state.config.wireguard_port == original_port


async def test_save_from_modal_persists_and_pops(wisp_app, monkeypatch):
    monkeypatch.setattr(
        "wisp.tui.screens.config.save_session_config",
        lambda *a, **k: "/tmp/wisp-test.toml",
    )
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        await _edit_port(wisp_app.screen, pilot, value="51234")

        await pilot.press("escape")
        await pilot.pause()
        await pilot.click("#btn-discard-save")
        await pilot.pause()

        assert isinstance(wisp_app.screen, MainMenuScreen)
        assert wisp_app.state.config.wireguard_port == 51234


async def test_reset_asks_when_dirty_and_discards(wisp_app):
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        original_port = wisp_app.state.config.wireguard_port
        await _edit_port(wisp_app.screen, pilot)

        wisp_app.screen.query_one("#btn-reset", Button).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(wisp_app.screen, ConfirmDiscardScreen)

        await pilot.click("#btn-discard-drop")
        await pilot.pause()

        assert isinstance(wisp_app.screen, ConfigScreen)
        port = wisp_app.screen.query_one("#input-wireguard-port", Input)
        assert port.value == str(original_port)
        assert wisp_app.state.config.wireguard_port == original_port
        assert wisp_app.screen._is_dirty() is False


async def _press_button(wisp_app, pilot, button_id: str) -> None:
    wisp_app.screen.query_one(button_id, Button).focus()
    await pilot.press("enter")
    await pilot.pause()


async def test_save_button_asks_first(wisp_app):
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        original_port = wisp_app.state.config.wireguard_port
        await _edit_port(wisp_app.screen, pilot)

        await _press_button(wisp_app, pilot, "#btn-save")

        modal = wisp_app.screen
        assert isinstance(modal, ConfirmActionScreen)

        await pilot.click("#btn-action-no")
        await pilot.pause()
        assert isinstance(wisp_app.screen, ConfigScreen)
        assert wisp_app.screen._is_dirty() is True
        assert wisp_app.state.config.wireguard_port == original_port


async def test_save_confirmed_persists_and_pops(wisp_app, monkeypatch):
    monkeypatch.setattr(
        "wisp.tui.screens.config.save_session_config",
        lambda *a, **k: "/tmp/wisp-test.toml",
    )
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        await _edit_port(wisp_app.screen, pilot, value="52111")

        await _press_button(wisp_app, pilot, "#btn-save")
        await pilot.click("#btn-action-yes")
        await pilot.pause()

        assert isinstance(wisp_app.screen, MainMenuScreen)
        assert wisp_app.state.config.wireguard_port == 52111


async def test_reset_clean_notifies_and_skips_modal(wisp_app):
    """With a clean form there is nothing to reload: reset must stay on the
    config screen (no modal) and raise an information notification."""
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        assert wisp_app.screen._is_dirty() is False

        await _press_button(wisp_app, pilot, "#btn-reset")

        assert isinstance(wisp_app.screen, ConfigScreen)

        # The notification is inspected via App._notifications: Textual's
        # toasts live in an overlay that is fiddly to query, and no other
        # test in this suite asserts notifications any other way.
        notifications = list(wisp_app._notifications)
        assert any(
            "Nothing to reset" in notification.message
            and notification.severity == "information"
            for notification in notifications
        )


async def test_arrows_move_focus_inside_confirm_modals(wisp_app, monkeypatch):
    monkeypatch.setattr(
        "wisp.tui.screens.config.save_session_config",
        lambda *a, **k: "/tmp/wisp-test.toml",
    )
    async with wisp_app.run_test(size=(100, 45)) as pilot:
        await _open_config(pilot, wisp_app)
        await _edit_port(wisp_app.screen, pilot)
        await _press_button(wisp_app, pilot, "#btn-save")
        await pilot.pause()

        modal = wisp_app.screen
        assert isinstance(modal, ConfirmActionScreen)
        no_btn = modal.query_one("#btn-action-no")
        yes_btn = modal.query_one("#btn-action-yes")

        assert wisp_app.focused is no_btn
        await pilot.press("down")
        assert wisp_app.focused is yes_btn
        await pilot.press("down")
        assert wisp_app.focused is no_btn
        await pilot.press("up")
        assert wisp_app.focused is yes_btn
        await pilot.press("left")
        assert wisp_app.focused is no_btn
        await pilot.press("right")
        assert wisp_app.focused is yes_btn

        # Enter on the focused confirm button still resolves the modal.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(wisp_app.screen, MainMenuScreen)
        assert wisp_app.state.config.wireguard_port == 51999
