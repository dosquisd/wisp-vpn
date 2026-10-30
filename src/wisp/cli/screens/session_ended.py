"""Screens for a session that ended outside this TUI.

Two things can take a tunnel away from a running TUI, and both used to leave the
tunnel view showing a live session that no longer existed:

- the in-VM watchdog destroyed the remote end (idle timeout or hard TTL), while
  the local interface is still up and quietly sending packets into a void;
- another local Wisp process (typically the OS-scheduled ``wisp destroy``) is
  tearing the session down right now.

Neither case can be pushed into the TUI: a scheduled teardown is a detached OS
process with no channel back. The TUI detects them by polling (handshake age and
the teardown notice), then lands on one of these screens instead of pretending
the session is healthy.
"""

from textual.app import ComposeResult
from textual.containers import CenterMiddle, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Static

from wisp.cli.screens.base import WispScreen
from wisp.cli.screens.shutdown import ShutdownScreen
from wisp.session import ActiveSession
from wisp.utils import logger
from wisp.utils.operations import read_teardown_notice


class SessionEndedScreen(WispScreen):
    """Explain that the remote end is gone and offer a state reconciliation.

    Cleanup is deliberately not automatic: the remote resources are already
    gone, so a destroy would at best find nothing. Running it on request keeps
    the user in control and lets ``delete_vm``'s refresh reconcile whatever the
    in-VM watchdog left behind (a stopped instance, a stale stack entry).
    """

    CSS = """
    #ended-title {
        text-align: center;
        margin-bottom: 0;
    }

    #ended-body {
        text-align: center;
        margin-bottom: 0;
    }

    #ended-note {
        text-align: center;
        color: #64748b;
        margin-top: 0;
    }

    #ended-buttons {
        height: 3;
        margin-top: 1;
    }

    #ended-buttons Button {
        width: 1fr;
    }
    """

    def __init__(self, session: ActiveSession | None = None) -> None:
        """Initialize the screen.

        Args:
            session (ActiveSession | None): Marker of the session that ended.
                When ``None``, the cleanup action is hidden: there is nothing to
                target without a provider and region.
        """
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card"):
                yield Static(
                    "[bold red]Session ended[/bold red]",
                    id="ended-title",
                    classes="title",
                )
                yield Static(self._body_text(), id="ended-body", classes="subtitle")
                yield Static(
                    "Cleanup only reconciles state; it is safe to run even when the "
                    "remote resources are already gone.",
                    id="ended-note",
                    classes="subtitle",
                )
                with Horizontal(id="ended-buttons", classes="btn-group"):
                    if self._session is not None:
                        yield Button(
                            "Clean up leftover state",
                            id="btn-ended-cleanup",
                            variant="warning",
                            classes="btn-primary",
                        )
                    yield Button(
                        "Return to menu",
                        id="btn-ended-menu",
                        variant="primary",
                        classes="btn-primary",
                    )
        yield Footer()

    def _body_text(self) -> str:
        """Build the explanation, naming the session when it is known."""
        session = self._session
        if session is None:
            return (
                "The local tunnel interface is up but no WireGuard handshake has "
                "arrived for several minutes. The remote end was most likely torn "
                "down on its own."
            )
        return (
            f"The local interface is up, but {session.provider.upper()}/"
            f"{session.region} has not answered a WireGuard handshake for several "
            "minutes. The remote end was most likely destroyed by the in-server "
            "watchdog: a cloud VM is powered off, or Wisp's interface is removed "
            "from a custom VPS."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-ended-cleanup" and self._session is not None:
            self.app.push_screen(
                ShutdownScreen(
                    reason="cleaning up after a remote session end",
                    orphan=self._session,
                )
            )
        elif event.button.id == "btn-ended-menu":
            self._return_to_menu()

    def _return_to_menu(self) -> None:
        """Go back to the main menu and refresh its orphan row."""
        self.app.switch_screen("main_menu")
        refresh = getattr(self.app, "_refresh_menu_orphan_row", None)
        if callable(refresh):
            self.app.call_after_refresh(refresh)


class ExternalTeardownScreen(WispScreen):
    """Wait for another Wisp process to finish destroying this session.

    The teardown notice is written by whoever holds the destroy lock. Watching
    the notice disappear is the cheapest reliable completion signal: it needs no
    socket, no shared event loop, and it still works when the TUI is the only
    process left running.
    """

    CSS = """
    #external-title {
        text-align: center;
        margin-bottom: 0;
    }

    #external-body {
        text-align: center;
        margin-bottom: 0;
    }

    #external-note {
        text-align: center;
        color: #64748b;
        margin-top: 0;
    }
    """

    def __init__(self, notice: dict) -> None:
        """Initialize the screen.

        Args:
            notice (dict): The teardown notice observed when the end was
                detected. Its ``pid`` identifies the process being watched.
        """
        super().__init__()
        self._notice = dict(notice)
        self._timer = None
        self._done = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card"):
                yield Static(
                    "[bold yellow]Destroying resources[/bold yellow]",
                    id="external-title",
                    classes="title",
                )
                yield Static(self._body_text(), id="external-body", classes="subtitle")
                yield Static(
                    "This screen returns to the menu automatically when the "
                    "destroy finishes.",
                    id="external-note",
                    classes="subtitle",
                )
        yield Footer()

    def _body_text(self) -> str:
        """Name the process and target being waited on."""
        provider = str(self._notice.get("provider", "")).upper()
        region = str(self._notice.get("region", ""))
        pid = self._notice.get("pid", "unknown")
        target = f"{provider}/{region}" if provider and region else "the session"
        return (
            f"Another Wisp process (pid {pid}) is destroying {target}.\n"
            "Starting a deployment now would collide with it, so wait for the "
            "destroy to finish."
        )

    def on_mount(self) -> None:
        """Start polling the teardown notice."""
        self._timer = self.set_interval(1, self._poll)

    def on_unmount(self) -> None:
        """Stop polling when the screen goes away."""
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.stop()

    def _poll(self) -> None:
        """Return to the menu once the other process finished."""
        if self._done:
            return
        notice = read_teardown_notice()
        if notice is not None and notice.get("pid") == self._notice.get("pid"):
            return

        self._done = True
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.stop()

        try:
            self.app.notify(
                "Resources destroyed by another Wisp process.",
                title="Session torn down",
                severity="information",
                timeout=8,
            )
        except Exception as exc:
            # Notifications are cosmetic; the screen transition still matters.
            logger.debug(f"Could not show the teardown notification: {exc}")

        self.app.switch_screen("main_menu")
        refresh = getattr(self.app, "_refresh_menu_orphan_row", None)
        if callable(refresh):
            self.app.call_after_refresh(refresh)
