"""Shutdown screen: releases cloud resources, with visible progress.

Destroying a cloud stack is blocking work that takes minutes, so it cannot run
on the event loop without freezing the interface and looking like a hang. This
screen runs the teardown in a worker thread and reports progress while it works.

It serves two flows:

- the active session: ``q`` / ``Ctrl+C`` on any screen, ``esc`` / ``d`` from the
  tunnel view, plus the ``atexit`` and signal paths in :mod:`wisp.session`.
  The teardown goes through :class:`~wisp.session.SessionGuard` so it happens
  exactly once, then the app exits.
- an orphaned session: leftovers of a previous run whose process is gone. There
  is no live session to guard, so the marker's provider and region are destroyed
  directly and the app returns to the menu afterwards.
"""

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, CenterMiddle, Vertical
from textual.widgets import Footer, Header, ProgressBar, Static

from wisp.config.settings import save_session_config
from wisp.session import ActiveSession, teardown_active_session
from wisp.tui.screens.base import WispScreen
from wisp.utils import logger


class ShutdownScreen(WispScreen):
    """Runs the session teardown with visible progress, then exits."""

    BINDINGS = [
        # Nothing to bind: the process is about to lose its resources whether or
        # not the user is patient, so an escape hatch here would only tempt
        # someone into leaking a VM.
        Binding("escape", "noop", "", show=False),
    ]

    CSS = """
    #shutdown-title {
        text-align: center;
        margin-bottom: 0;
    }

    #shutdown-subtitle {
        text-align: center;
        margin-bottom: 0;
    }

    #shutdown-status {
        text-align: center;
        margin-top: 0;
        margin-bottom: 0;
        height: 2;
        color: #38bdf8;
    }

    #shutdown-note {
        text-align: center;
        color: #64748b;
        margin-top: 0;
    }

    #shutdown-progress-center {
        width: 100%;
    }

    #shutdown-progress {
        width: auto;
    }
    """

    def __init__(
        self,
        reason: str = "requested",
        *,
        exit_after: bool = False,
        orphan: ActiveSession | None = None,
    ) -> None:
        """Initialize the screen.

        Args:
            reason (str): Short explanation of why the shutdown was requested.
            exit_after (bool): Exit the app once the teardown finishes. The
                normal destroy flow returns to the main menu instead.
            orphan (ActiveSession | None): Marker of the abandoned session to
                clean up. When given, the teardown targets those resources
                directly instead of this process's session guard.
        """
        super().__init__()
        self._reason = reason
        self._exit_after = exit_after
        self._orphan = orphan
        self._failed = False

    def compose(self) -> ComposeResult:
        cleaning = self._orphan is not None
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card"):
                yield Static(
                    "[bold yellow]Cleaning up orphaned resources[/bold yellow]"
                    if cleaning
                    else "[bold yellow]Destroying tunnel[/bold yellow]",
                    id="shutdown-title",
                    classes="title",
                )
                yield Static(
                    "A previous process left resources active. "
                    "They are being cleaned up now."
                    if cleaning
                    else (
                        "Cloud resources are being destroyed. "
                        "Please wait until complete."
                    ),
                    id="shutdown-subtitle",
                    classes="subtitle",
                )
                with Center(id="shutdown-progress-center"):
                    yield ProgressBar(id="shutdown-progress", total=100, show_eta=False)
                yield Static("Starting teardown...", id="shutdown-status")
                yield Static(
                    "Returning to the menu when finished."
                    if cleaning
                    else "The app will exit when finished.",
                    id="shutdown-note",
                    classes="subtitle",
                )
        yield Footer()

    def on_mount(self) -> None:
        """Start the teardown worker."""
        self.start_teardown()

    def action_noop(self) -> None:
        """Intentionally do nothing for the escape binding."""

    @work(thread=True)
    def start_teardown(self) -> None:
        """Run the teardown off the event loop.

        Two cases, and the difference matters: a live session is released
        through its guard so the teardown runs exactly once even if a signal
        lands mid-flight, while an orphan has no guard (its process is gone) and
        is destroyed directly from the marker's provider and region.
        """
        if self._orphan is not None:
            self._teardown_orphan()
            return

        guard = self.app.state.session_guard
        if not guard.armed:
            logger.info("No active session to tear down")
            self.app.call_from_thread(self._finish, failed=False)
            return

        try:
            self.app.call_from_thread(self._set_status, "Releasing resources...")
            # The verdict is shutdown()'s return value — checking guard.armed
            # here would always read False, since shutdown() marks the guard
            # as finished by design. False means the teardown raised (the
            # cause is in guard.last_error) or a concurrent trigger won.
            failed = not guard.shutdown(self._reason)
        except Exception as exc:
            logger.error(f"Teardown raised: {exc}")
            self.app.call_from_thread(self._finish, failed=True, detail=str(exc))
            return

        self.app.call_from_thread(
            self._finish, failed=failed, detail=guard.last_error or ""
        )

    def _teardown_orphan(self) -> None:
        """Destroy the resources of a session left behind by a dead process."""
        orphan = self._orphan
        assert orphan is not None  # guarded by the caller
        try:
            self.app.call_from_thread(
                self._set_status,
                f"Destroying resources for {orphan.provider.upper()}/{orphan.region}...",
            )
            teardown_active_session(
                orphan.provider,
                orphan.region,
                on_progress=lambda msg, _p: self.app.call_from_thread(
                    self._set_status, msg
                ),
            )
        except Exception as exc:
            logger.error(f"Orphan teardown raised: {exc}")
            self.app.call_from_thread(self._finish, failed=True, detail=str(exc))
            return
        self.app.call_from_thread(self._finish, failed=False)

    def _set_status(self, message: str) -> None:
        """Update the status line and nudge the progress bar.

        Args:
            message (str): Status text to display.
        """
        try:
            self.query_one("#shutdown-status", Static).update(message)
            pbar = self.query_one("#shutdown-progress", ProgressBar)
            # Indeterminate-feeling progress: the real percentage is not
            # meaningful across providers, but movement signals "alive".
            pbar.progress = min(99, pbar.progress + 7)
        except Exception:
            pass

    def _finish(self, failed: bool, detail: str = "") -> None:
        """Show the outcome and exit.

        Args:
            failed (bool): True when the teardown did not complete cleanly.
            detail (str): Extra detail to surface when ``failed``.
        """
        self._failed = failed
        cleaning = self._orphan is not None
        try:
            title = self.query_one("#shutdown-title", Static)
            note = self.query_one("#shutdown-note", Static)
            pbar = self.query_one("#shutdown-progress", ProgressBar)
            status = self.query_one("#shutdown-status", Static)
            if failed:
                title.update("[bold red]✗ Teardown incomplete[/bold red]")
                status.update(
                    detail or "Resources may still be active; check your cloud console."
                )
                note.update(
                    "Returning to the menu; you can retry or run "
                    "'wisp destroy' manually."
                )
                pbar.progress = 100
            else:
                title.update("[bold green]✓ Resources released[/bold green]")
                if cleaning:
                    status.update("Orphaned resources cleaned up.")
                    note.update("Returning to the menu...")
                else:
                    status.update("Tunnel destroyed. Done.")
                    note.update("All done.")
                pbar.progress = 100
        except Exception:
            pass

        self.set_timer(1.2, self._exit_now)

    def _exit_now(self) -> None:
        """Close out: exit the app, or return to the main menu."""
        if self._exit_after:
            self.app.exit()
            return
        self.app.switch_screen("main_menu")


def request_shutdown(app, reason: str) -> None:
    """Ask the app to tear down any active session and return to the menu.

    Safe to call from any screen and from any thread: it only pushes the
    shutdown screen, which owns the actual teardown.

    Args:
        app: The running :class:`~wisp.tui.app.WispApp`.
        reason (str): Short explanation of why the shutdown was requested.
    """
    try:
        app.push_screen(ShutdownScreen(reason=reason))
    except Exception as exc:
        # If the UI cannot show the teardown, fall back to the atexit hook so
        # the resources are still released even if the user sees nothing.
        logger.error(f"Could not show the shutdown screen ({exc}); exiting anyway")
        app.exit()


def confirmed_destroy(app, choice: tuple[bool, bool] | None, reason: str) -> bool:
    """Honour a :class:`ConfirmDestroyScreen` verdict.

    Applies the "Don't ask again" switch by persisting
    ``[general].confirm_destroy = false``, then routes the teardown through
    :func:`request_shutdown` so every destroy trigger shares the same guard.

    Args:
        app: The running :class:`~wisp.tui.app.WispApp`.
        choice: The modal's result, ``(confirmed, no_ask)`` or ``None``.
        reason (str): Reason forwarded to ``request_shutdown``.

    Returns:
        bool: True when the destroy was confirmed and started; False when the
            user backed out.
    """
    if choice is None or not choice[0]:
        return False

    _, no_ask = choice
    if no_ask:
        state = app.state
        state.config.confirm_destroy = False
        try:
            save_session_config(state.config)
        except Exception as exc:
            logger.warning(f"Could not persist confirm_destroy: {exc}")

    request_shutdown(app, reason)
    return True
