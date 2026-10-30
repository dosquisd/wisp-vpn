"""Root Textual application and global styles for the Wisp TUI."""

import os
import time

from textual.app import App
from textual.binding import Binding

from wisp.cli.screens import ConfigScreen, DeployScreen, MainMenuScreen, TunnelScreen
from wisp.cli.screens.modal import ConfirmDestroyScreen
from wisp.cli.screens.shutdown import confirmed_destroy, request_shutdown
from wisp.cli.screens.tunnel import (
    HEALTH_LIVE,
    HEALTH_LOCAL_DOWN,
    HEALTH_WAITING,
    health_state,
)
from wisp.cli.session_boot import arm_session_guard
from wisp.cli.state import AppState
from wisp.session import (
    ActiveSession,
    find_orphaned_session,
    install_signal_handlers,
    process_alive,
    read_active_session,
    register_atexit,
)
from wisp.utils.privileges import root_invocation


class WispApp(App):
    """The Wisp terminal UI application.

    Owns the shared :class:`~wisp.cli.state.AppState`, defines global CSS and
    key bindings, and installs the main-menu, config, and deploy screens on
    mount.
    """

    TITLE = "Wisp"
    SUB_TITLE = "Ephemeral WireGuard VPNs on your own cloud"

    BINDINGS = [
        Binding("q", "quit", "Exit", show=True),
        Binding("ctrl+c", "quit", "Exit", show=False),
    ]

    CSS = """
    Screen {
        background: #0b0f19;
        color: #f1f5f9;
        align: center middle;
    }

    Header {
        background: #111827;
        color: #38bdf8;
        dock: top;
        height: 1;
    }

    Footer {
        background: #111827;
        dock: bottom;
        height: 1;
    }

    .card {
        background: #1e293b;
        border: round #38bdf8;
        padding: 1 2;
        width: 74;
        height: auto;
        /* Must stay at 100%: any smaller percentage truncates the auto height
           by a row or two, and `overflow-y: auto` then renders a scrollbar for
           that gap alone -- a scroll that reveals nothing but padding. At 100%
           the bar disappears once the content fits and still appears when the
           terminal is genuinely too short to show everything. */
        max-height: 100%;
        overflow-y: auto;
    }

    .title {
        text-style: bold;
        color: #38bdf8;
        text-align: center;
        margin-bottom: 1;
    }

    .subtitle {
        color: #94a3b8;
        text-align: center;
        margin-bottom: 1;
    }

    .btn-primary {
        background: #0284c7;
        color: #ffffff;
        border: none;
        width: 100%;
        margin-top: 1;
    }

    .btn-primary:hover {
        background: #0369a1;
    }

    .btn-secondary {
        background: #334155;
        color: #f8fafc;
        border: none;
        width: 100%;
        margin-top: 1;
    }

    .btn-secondary:hover {
        background: #475569;
    }

    .btn-danger {
        background: #dc2626;
        color: #ffffff;
        border: none;
        width: 100%;
        margin-top: 1;
    }

    .btn-danger:hover {
        background: #b91c1c;
    }

    Input:focus, Switch:focus {
        border: tall #38bdf8;
    }

    Button:focus {
        text-style: bold reverse;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.state = AppState()

    def on_mount(self) -> None:
        """Install the app screens and show the main menu."""
        try:
            from wisp.config.settings import describe_config_sources
            from wisp.utils import logger

            logger.info(describe_config_sources())
        except Exception:
            pass  # purely informational; never block startup over this

        self._configure_shutdown()

        self.install_screen(MainMenuScreen(), name="main_menu")
        self.install_screen(ConfigScreen(), name="config")
        self.install_screen(DeployScreen(), name="deploy")
        self.push_screen("main_menu")
        self._offer_orphan_cleanup()
        # Adoption runs after the first refresh so the menu is laid out before
        # it can be replaced by the tunnel view. Whether the session is actually
        # adoptable is decided by `_can_adopt`, which `_offer_orphan_cleanup`
        # also consults: a live tunnel is never advertised as an orphan.
        self.call_after_refresh(self._maybe_adopt_live_session)

    def thread_safe_call(self, message: str) -> None:
        """Show a one-line status on whichever screen is active, from any thread.

        Teardown runs on a worker thread long after the screen that started it was
        swapped out, so it cannot hold a reference to it. Resolving the widget at
        call time keeps the teardown tied to the app instead.

        Args:
            message (str): Status text to display.
        """
        if self._exit:
            return
        for widget_id in ("#shutdown-status", "#tunnel-health", "#progress-status-msg"):
            for screen in self.screen_stack:
                try:
                    widget = screen.query_one(widget_id)
                except Exception:
                    continue
                self.call_from_thread(widget.update, message)
                return

    def _configure_shutdown(self) -> None:
        """Wire every shutdown trigger into the session guard.

        Ctrl+C is deliberately *not* routed through a signal handler: Textual's
        driver clears the terminal's ``ISIG`` flag, so Ctrl+C arrives as a key
        event and lands on the ``ctrl+c`` binding below. Keeping it a key event
        means the teardown runs through the same UI path as ``q``, with visible
        progress, instead of interrupting the event loop from a signal context.

        Signals sent from outside (a shell ``kill``, a supervisor, a closing
        terminal) still have to be caught, so ``SIGINT``/``SIGTERM`` go to the
        guard, and ``atexit`` covers normal interpreter exit on all platforms.
        """
        install_signal_handlers(self.state.session_guard)
        register_atexit(self.state.session_guard)

    def action_quit(self) -> None:
        """Quit, destroying any live tunnel — asking first, unless disabled.

        Reached by both ``q`` and ``Ctrl+C`` from every screen. When a tunnel is
        up the session guard is armed and quitting destroys it, which is
        irreversible, so the same ``ConfirmDestroyScreen`` as the destroy button
        asks first (honouring ``[general] confirm_destroy``). Elsewhere there is
        nothing live and the app quits straight away.
        """
        state = self.state
        if isinstance(self.screen, ConfirmDestroyScreen):
            return
        if state.session_guard.armed and state.config.confirm_destroy:
            self.push_screen(ConfirmDestroyScreen(), self._on_quit_destroy_choice)
        else:
            self.exit()

    def _on_quit_destroy_choice(self, choice: tuple[bool, bool] | None) -> None:
        """Apply the destroy modal's verdict when quitting with a live session."""
        confirmed_destroy(
            self,
            choice,
            "tunnel destroyed on quit",
        )

    def _offer_orphan_cleanup(self) -> None:
        """Offer to clean up a session left behind by a previous run.

        A crash, a ``kill -9`` or a power loss leave the marker file on disk.
        Its owning process is gone, so its VM is still running and still
        billing; this surfaces that instead of leaking it silently.

        A marker whose tunnel is provably still live is not offered here,
        because it is not an orphan: startup adoption takes the user to the
        tunnel view instead. Warning someone to "clean up" the session they are
        about to see running would be actively misleading — and acting on it
        would destroy a working tunnel.

        An interface that is up but reports no handshake age is **not**
        evidence of death either, so it never reaches the destructive offer. The
        usual cause is a daemon too old to report the field, which is worth
        saying out loud: the alternative is a user deleting a healthy session
        because the menu told them to.
        """
        from wisp.utils import logger

        orphan = find_orphaned_session()
        if orphan is None:
            return

        health = self._adoption_health(orphan)
        if health is None:
            return

        if health == HEALTH_LIVE:
            logger.info(
                f"Session left by pid {orphan.pid} is still live; recovering it "
                "instead of offering a cleanup."
            )
            return

        if health == HEALTH_WAITING:
            logger.warning(
                f"Session {orphan.summary()} has its interface up, but the local "
                "daemon reported no handshake age; not offering a cleanup we "
                "cannot justify."
            )
            try:
                self.notify(
                    f"Session {orphan.summary()} is up, but the local daemon did "
                    "not report its handshake age.\n"
                    "Either the tunnel has not completed a handshake yet, or the "
                    "daemon is outdated. Reinstall it with "
                    f"`{root_invocation('daemon install')}`.",
                    title="Session state unknown",
                    severity="warning",
                    timeout=15,
                )
            except Exception:
                pass  # notifications are cosmetic; never block startup
            return

        logger.warning(
            f"Found a session left behind by a dead process: {orphan.summary()}"
        )
        try:
            self.notify(
                f"Orphaned tunnel found: {orphan.summary()}.\n"
                "Cloud resources may still be incurring charges; clean them up "
                "using option 4 in the main menu.",
                title="Orphaned session",
                severity="warning",
                timeout=15,
            )
        except Exception:
            # Notifications are cosmetic; never let them block startup.
            pass

        # The notification alone expires, so the menu also carries a durable
        # row for this: the warning is only useful while it can still be acted
        # on, and the marker outlives the process that would have used it.
        self.call_after_refresh(self._refresh_menu_orphan_row)

    def _adoption_health(self, session: ActiveSession) -> str | None:
        """Classify how alive a leftover session is, for the startup decisions.

        :func:`~wisp.cli.screens.tunnel.health_state` is the single source of
        truth for "is this tunnel real", so the menu and the tunnel view cannot
        disagree about the same evidence. The only extra rule is ownership: a
        marker whose process is still running is not ours to judge.

        Args:
            session (ActiveSession): Marker read from disk.

        Returns:
            str | None: A ``health_state`` value, or ``None`` when the owning
            process is still alive. An unreachable daemon is reported as
            ``HEALTH_LOCAL_DOWN`` because nothing proves the interface is up.
        """
        from wisp.utils import logger

        if session.pid != os.getpid() and process_alive(session.pid):
            logger.debug(f"Not judging tunnel: process {session.pid} still owns it")
            return None

        try:
            from wisp.wireguard import status_wireguard_client

            response = status_wireguard_client()
        except Exception as exc:
            logger.debug(f"Could not classify the leftover tunnel: {exc}")
            return HEALTH_LOCAL_DOWN

        # ``elapsed=0.0`` on purpose: a startup check has no "this view has been
        # watching for N seconds" context, so an interface with no handshake yet
        # must stay unknown instead of being declared dead. The tunnel view
        # applies the grace window once it is actually watching.
        return health_state(response, 0.0)

    def _can_adopt(self, session: ActiveSession) -> bool:
        """Return whether ``session`` is a tunnel that is provably still live.

        Adoption is deliberately evidence-based: the marker must belong to a
        dead process *and* the daemon must report an interface with a fresh
        handshake. A live owner is never hijacked, and a stale tunnel is left to
        the daemon's own startup cleanup.

        The probe is a local socket round-trip, so calling this more than once
        during startup costs nothing worth caching, and caching would freeze an
        answer the daemon may already have changed.

        Args:
            session (ActiveSession): Marker read from disk.

        Returns:
            bool: ``True`` when the tunnel view should take over this session.
        """
        return self._adoption_health(session) == HEALTH_LIVE

    def _maybe_adopt_live_session(self) -> None:
        """Reattach to a tunnel that is still up from a previous run.

        A crash or a closed terminal can leave the WireGuard interface (and the
        cloud resources behind it) alive while the marker's owning process is
        gone. Starting the TUI in that state used to show the main menu as if
        nothing were running, which is exactly when a user is most likely to
        deploy a second, overlapping session.
        """
        from wisp.utils import logger

        session = read_active_session()
        if session is None or not self._can_adopt(session):
            return

        logger.info(f"Adopting live tunnel left by pid {session.pid}")
        self._adopt_session(session)

    def _adopt_session(self, session) -> None:
        """Populate the app state and show the tunnel view for ``session``.

        Args:
            session (ActiveSession): Marker of the tunnel being adopted.
        """
        state = self.state
        state.provider_name = session.provider
        state.selected_region = session.region
        vps = session.vps or {}
        if vps:
            for key in ("host", "ssh_user", "ssh_port", "ssh_key_path"):
                if key in vps:
                    setattr(state.vps_credentials, key, vps[key])

        # The marker and the OS one-shot already exist: rewriting either would
        # reset started_at/expires_at and silently extend the session.
        arm_session_guard(
            self,
            provider=session.provider,
            region=session.region,
            ttl_minutes=session.ttl_minutes,
            vps=session.vps or None,
            write_marker=False,
            schedule_watchdog=False,
        )

        # The OS one-shot may already have fired, or may be about to. Scheduling
        # the deadline inside this event loop lets the live TUI release the
        # session itself instead of racing a detached process for the lock.
        if session.has_ttl:
            self.set_timer(
                max(0.1, session.expires_at - time.time()),
                lambda: request_shutdown(self, "session TTL expired"),
            )

        self.switch_screen(TunnelScreen(adopted=session))
        try:
            self.notify(
                f"Recovered the {session.provider.upper()}/{session.region} "
                "tunnel from a previous run.\n"
                "Quitting now destroys it.",
                title="Session recovered",
                severity="information",
                timeout=12,
            )
        except Exception:
            # Notifications are cosmetic; never block startup over one.
            pass

    def _refresh_menu_orphan_row(self) -> None:
        """Ask the main menu to re-evaluate its orphan-cleanup row."""
        try:
            menu = self.get_screen("main_menu")
        except Exception:
            return
        refresher = getattr(menu, "refresh_orphan_row", None)
        if callable(refresher):
            refresher()
