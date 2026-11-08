"""Main menu screen: entry point with a live configuration summary."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import CenterMiddle, Vertical
from textual.widgets import Button, Footer, Header, Static

from wisp.session import find_orphaned_session
from wisp.tui.screens.base import WispScreen
from wisp.tui.screens.shutdown import ShutdownScreen


class MainMenuScreen(WispScreen):
    """Landing screen; shows current config and navigates to other screens."""

    BINDINGS = [
        Binding("1", "deploy", "Deploy", show=True),
        Binding("2", "config", "Settings", show=True),
        Binding("3", "quit", "Exit", show=True),
        Binding("4", "cleanup_orphan", "Cleanup Orphan", show=False),
    ]

    # Rendered with the official figlet "ANSI Shadow" W/I/S/P glyphs
    # (no shade characters, every line padded to the same 29-column width
    # so the letters align exactly on any terminal).
    BANNER = (
        "[bold cyan]██╗    ██╗██╗███████╗██████╗ \n"
        "██║    ██║██║██╔════╝██╔══██╗\n"
        "██║ █╗ ██║██║███████╗██████╔╝\n"
        "██║███╗██║██║╚════██║██╔═══╝ \n"
        "╚███╔███╔╝██║███████║██║     \n"
        " ╚══╝╚══╝ ╚═╝╚══════╝╚═╝     [/bold cyan]"
    )

    # Scoped to this screen only (doesn't touch .title/.subtitle/.btn-* on
    # other screens, which legitimately need the roomier default spacing).
    # This menu is the one screen where scrolling to reach "Exit" is
    # actually bad UX — it's the exit door — so every row here is trimmed
    # to make the whole card fit without scrolling on a normal terminal.
    # Padding drops to 0 vertical (the round border already provides
    # visual separation) to offset the banner's sixth line.
    CSS = """
    MainMenuScreen .card {
        padding: 0 2;
    }

    MainMenuScreen .title {
        margin-bottom: 0;
    }

    MainMenuScreen .subtitle {
        margin-bottom: 0;
    }

    #status-preview {
        margin-bottom: 0;
    }

    /* Textual 8 buttons are 1 row tall, so the default `.btn-*` margin-top: 1
       made every option occupy a 2-row pitch and the menu read as a sparse
       list. Pinning the margin to 0 halves that pitch; the solid backgrounds
       still separate the rows, so no gap is needed. */
    #btn-deploy, #btn-config, #btn-quit {
        margin-top: 0;
    }

    #orphan-warning {
        display: none;
        height: auto;
        color: #fbbf24;
        text-align: center;
        margin-bottom: 1;
    }

    #btn-orphan-cleanup {
        display: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card"):
                yield Static(self.BANNER, classes="title")
                yield Static("Ephemeral WireGuard VPNs on demand", classes="subtitle")
                yield Static("", id="orphan-warning", classes="orphan-warning")
                yield Static(self._get_status_text(), id="status-preview")
                yield Button(
                    "1. Deploy VPN",
                    id="btn-deploy",
                    variant="primary",
                    classes="btn-primary",
                )
                yield Button(
                    "2. Settings",
                    id="btn-config",
                    variant="default",
                    classes="btn-secondary",
                )
                yield Button(
                    "3. Exit",
                    id="btn-quit",
                    variant="error",
                    classes="btn-danger",
                )
                # Composed LAST, on purpose: Textual focuses the first focusable
                # widget on mount, and this one is usually hidden. Yielded first
                # it would win the autofocus, and since a `display: none` widget
                # is still walked by `focus_next`, the very first arrow press
                # would strand focus on an invisible button and trap the user.
                # `can_focus=False` makes that structurally impossible; the row
                # stays reachable through the `4` binding and the mouse.
                orphan_button = Button(
                    "Clean up orphaned resources",
                    id="btn-orphan-cleanup",
                    variant="warning",
                    classes="btn-danger",
                )
                orphan_button.can_focus = False
                yield orphan_button
        yield Footer()

    def on_screen_resume(self) -> None:
        self.update_status()
        self.refresh_orphan_row()

    def refresh_orphan_row(self) -> None:
        """Show the orphan-cleanup row only while an orphan actually exists.

        The row is what makes the startup warning actionable: a marker whose
        owning process is gone means a VM is still running and still billing,
        and this is the one screen reachable without leaving the TUI.
        """
        try:
            warning = self.query_one("#orphan-warning", Static)
            button = self.query_one("#btn-orphan-cleanup", Button)
        except Exception:
            return

        orphan = find_orphaned_session()
        if orphan is None:
            warning.display = False
            button.display = False
            return

        warning.display = True
        button.display = True
        warning.update(
            f"[bold yellow]⚠ Orphaned session[/bold yellow]\n"
            f"[dim]{orphan.provider.upper()}/{orphan.region} "
            f"started by process {orphan.pid}, "
            f"which no longer exists. Cloud resources may still be running.[/dim]"
        )
        # The key hint is in the label because the footer binding is hidden:
        # the row appears conditionally, so a static footer entry would either
        # advertise a key that does nothing or lie about what exists.
        button.label = f"4. Clean up orphaned resources ({orphan.provider.upper()})"

    def action_cleanup_orphan(self) -> None:
        """Destroy the resources left behind by a dead run."""
        orphan = find_orphaned_session()
        if orphan is None:
            self.refresh_orphan_row()
            return
        self.app.push_screen(
            ShutdownScreen(
                reason="orphaned session cleanup",
                exit_after=False,
                orphan=orphan,
            )
        )

    def update_status(self) -> None:
        """Refresh the configuration summary widget."""
        try:
            status_widget = self.query_one("#status-preview", Static)
            status_widget.update(self._get_status_text())
        except Exception:
            pass

    def _get_status_text(self) -> str:
        """Build the provider/region/config summary shown on the menu."""
        state = self.app.state  # type: ignore[attr-defined]
        cfg = state.config
        port_str = (
            f"UDP {cfg.wireguard_port}"
            if cfg.wireguard_port > 0
            else "Random (49152-65535)"
        )
        ip_mode = (
            "My current IP only (/32)" if cfg.force_current_ip else "Any IP (0.0.0.0/0)"
        )
        return (
            f"[dim]Provider:[/dim] [cyan]{state.provider_name.upper()}[/cyan]   "
            f"[dim]Region:[/dim] [yellow]{state.selected_region}[/yellow]\n"
            f"[dim]Timeout:[/dim] [white]{cfg.vm_boot_timeout}s[/white]   "
            f"[dim]Port:[/dim] [white]{port_str}[/white]\n"
            f"[dim]Firewall:[/dim] [white]{ip_mode}[/white]"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-deploy":
            self.action_deploy()
        elif event.button.id == "btn-config":
            self.action_config()
        elif event.button.id == "btn-orphan-cleanup":
            self.action_cleanup_orphan()
        elif event.button.id == "btn-quit":
            self.action_quit()

    def action_deploy(self) -> None:
        """Open the deploy wizard.

        A live tunnel is unreachable from here: once a deployment succeeds the
        tunnel view replaces this screen, and every exit from it destroys the
        session. The menu only ever shows while nothing is running.
        """
        self.app.push_screen("deploy")

    def action_config(self) -> None:
        self.app.push_screen("config")

    def action_quit(self) -> None:
        """Use the app-level quit flow for keyboard and button exits."""
        self.app.action_quit()
