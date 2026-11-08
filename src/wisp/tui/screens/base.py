"""Shared base screen for the Wisp TUI.

Textual only cycles focus with Tab/Shift+Tab by default. Wisp's screens are
short forms, so arrow keys feel more natural: Up/Down move between widgets in
DOM order (row by row), and Left/Right do the same, which is what most people
try first inside a button group like *Iniciar despliegue / Cancelar*.

``WispScreen`` adds that everywhere without touching widgets that already use
the arrows for their own thing — an ``Input`` consumes ``left``/``right`` to
move the text cursor, and a ``Select`` consumes ``up``/``down`` to move between
its options. Textual resolves bindings from the focused widget upwards, so both
already shadow these screen-level bindings while they are focused; the guard in
``_arrows_owned_by_focused_widget`` makes the intent explicit and future-proof.

All screens should inherit from this instead of ``textual.screen.Screen``.
Textual merges ``BINDINGS`` across the MRO, so a subclass's own bindings
(``escape``, ``1``/``2``/``3``, etc.) keep working unchanged.
"""

from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Input, Select


class WispScreen(Screen):
    """Base screen adding the arrow keys as focus navigation aliases."""

    BINDINGS = [
        Binding("left", "focus_previous_widget", "Anterior", show=False),
        Binding("right", "focus_next_widget", "Siguiente", show=False),
        Binding("down", "focus_next_widget", "Siguiente", show=False),
        Binding("up", "focus_previous_widget", "Anterior", show=False),
    ]

    def action_focus_next_widget(self) -> None:
        if self._arrows_owned_by_focused_widget():
            return
        self.focus_next()

    def action_focus_previous_widget(self) -> None:
        if self._arrows_owned_by_focused_widget():
            return
        self.focus_previous()

    def _arrows_owned_by_focused_widget(self) -> bool:
        """True when the focused widget needs the arrow keys for itself.

        An ``Input`` uses ``left``/``right`` to move the text cursor and a
        ``Select`` uses ``up``/``down`` to move between its own options (open
        or not), so we back off and let them handle the arrows instead of
        stealing focus mid-edit / mid-selection.
        """
        return isinstance(self.focused, (Input, Select))
