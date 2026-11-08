"""Reusable confirmation modals for the Wisp TUI.

Two questions, both modal (they dim the screen below) and both resolved through
``dismiss``:

- :class:`ConfirmDiscardScreen` — what to do with unsaved form edits when the
  user tries to leave the config screen. Returns ``"save"``, ``"discard"``, or
  ``None`` (cancel/escape).
- :class:`ConfirmDestroyScreen` — whether to tear down the active VPN when the
  user tries to leave the tunnel view. Returns ``(confirmed, no_ask)``; the
  second flag becomes ``[general] confirm_destroy`` and is persisted by the
  caller, so "Don't ask again" survives restarts.
"""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, CenterMiddle, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Label, Static, Switch


class _ConfirmModal(ModalScreen):
    """Shared chrome for the question boxes: dimmed backdrop + centred card.

    The arrow keys navigate between the modal's focusable widgets (Up/Down and
    Left/Right both cycle, like :class:`~wisp.tui.screens.base.WispScreen`),
    so keyboard-only users never depend on Tab. The modal contents are only
    buttons/one switch, so nothing here needs the arrow keys for itself.
    """

    BINDINGS = [
        Binding("left", "focus_previous_widget", "Previous", show=False),
        Binding("right", "focus_next_widget", "Next", show=False),
        Binding("down", "focus_next_widget", "Next", show=False),
        Binding("up", "focus_previous_widget", "Previous", show=False),
    ]

    def action_focus_next_widget(self) -> None:
        self.focus_next()

    def action_focus_previous_widget(self) -> None:
        self.focus_previous()

    CSS = """
    .modal-card {
        width: 62;
        height: auto;
        max-height: 100%;
        overflow-y: auto;
        background: #0b0f19;
        border: thick #334155;
        padding: 1 2;
    }

    .modal-title {
        color: #38bdf8;
        text-align: center;
        margin-bottom: 1;
    }

    .modal-body {
        color: #94a3b8;
        margin-bottom: 1;
    }

    .modal-actions Button {
        margin-right: 1;
        width: auto;
    }

    .modal-option {
        height: auto;
        min-height: 3;
        margin-bottom: 1;
        align: center middle;
    }

    .modal-option Label {
        color: #94a3b8;
        margin-right: 2;
    }
    """


class ConfirmActionScreen(_ConfirmModal):
    """Generic yes/no modal for a labelled destructive-ish action.

    Parameters drive the copy, so it can cover actions like saving or resetting
    settings. Resolves to ``True`` (confirmed) or ``False``/``None`` (dismissed).
    """

    BINDINGS = [
        Binding("escape", "decline", "Cancel", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        body: str,
        confirm_label: str,
        danger: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label
        self._danger = danger

    def compose(self) -> ComposeResult:
        confirm_variant = "error" if self._danger else "primary"
        confirm_classes = "btn-danger" if self._danger else "btn-primary"
        with CenterMiddle():
            with Vertical(classes="modal-card"):
                yield Static(self._title, classes="modal-title")
                yield Static(self._body, classes="modal-body")
                with Center(classes="modal-actions"):
                    yield Button(
                        "Cancel",
                        id="btn-action-no",
                        variant="default",
                        classes="btn-secondary",
                    )
                    yield Button(
                        self._confirm_label,
                        id="btn-action-yes",
                        variant=confirm_variant,
                        classes=confirm_classes,
                    )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-action-yes")

    def action_decline(self) -> None:
        self.dismiss(False)


class ConfirmDiscardScreen(_ConfirmModal):
    """Ask whether to keep, drop, or cancel while unsaved edits exist."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancelar", show=False),
    ]

    def compose(self) -> ComposeResult:
        with CenterMiddle():
            with Vertical(classes="modal-card"):
                yield Static(
                    "Unsaved changes",
                    id="confirm-discard-title",
                    classes="modal-title",
                )
                yield Static(
                    "There are unsaved changes that have not been written to wisp.toml.\n"
                    "If you discard now, they will be lost.",
                    id="confirm-discard-body",
                    classes="modal-body",
                )
                with Center(classes="modal-actions"):
                    yield Button(
                        "Save",
                        id="btn-discard-save",
                        variant="primary",
                        classes="btn-primary",
                    )
                    yield Button(
                        "Discard",
                        id="btn-discard-drop",
                        variant="default",
                        classes="btn-secondary",
                    )
                    yield Button(
                        "Cancel",
                        id="btn-discard-cancel",
                        variant="default",
                        classes="btn-secondary",
                    )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        result = {
            "btn-discard-save": "save",
            "btn-discard-drop": "discard",
            "btn-discard-cancel": None,
        }.get(event.button.id)
        self.dismiss(result)

    def action_cancel(self) -> None:
        """Escape or the Cancel button: do nothing, keep the edits."""
        self.dismiss(None)


class ConfirmDestroyScreen(_ConfirmModal):
    """Confirm tearing down the active VPN before leaving the tunnel view."""

    BINDINGS = [
        Binding("escape", "decline", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with CenterMiddle():
            with Vertical(classes="modal-card"):
                yield Static(
                    "Destroy active VPN?",
                    id="confirm-destroy-title",
                    classes="modal-title",
                )
                yield Static(
                    "The cloud VM will be deleted and the WireGuard tunnel closed.\n"
                    "You will stop incurring charges.",
                    id="confirm-destroy-body",
                    classes="modal-body",
                )
                with Horizontal(classes="modal-option"):
                    yield Label("Don't ask again for this session:")
                    yield Switch(id="switch-no-ask-destroy", value=False)
                with Center(classes="modal-actions"):
                    yield Button(
                        "No, Cancel",
                        id="btn-destroy-no",
                        variant="default",
                        classes="btn-secondary",
                    )
                    yield Button(
                        "Yes, Destroy",
                        id="btn-destroy-yes",
                        variant="error",
                        classes="btn-danger",
                    )
        yield Footer()

    def _choice(self, confirmed: bool) -> tuple[bool, bool]:
        no_ask = self.query_one("#switch-no-ask-destroy", Switch).value
        return (confirmed, no_ask)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._choice(event.button.id == "btn-destroy-yes"))

    def action_decline(self) -> None:
        """Escape or Cancel: keep the tunnel up and stay on the tunnel view."""
        self.dismiss((False, False))
