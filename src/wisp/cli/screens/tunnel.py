"""Active tunnel screen: the persistent view shown while a VPN is up.

Mirrors how a tunnel tool keeps a live view for as long as the tunnel exists,
instead of a one-shot result screen that scrolls away. It shows what the
session is (provider, region, endpoints), how long it has been up, and whether
the local interface is still healthy.

This screen is a terminal state, not a stop on the way somewhere else: the VPN
is ephemeral by design, so every way out of it destroys the tunnel and its cloud
resources. ``q``, ``Ctrl+C``, ``esc``, ``d`` and the button all release the
session through :class:`~wisp.session.SessionGuard`, and closing the terminal
does too via ``SIGHUP``. There is deliberately no "go back and leave it running"
path -- a tunnel nobody is watching is a VM nobody tears down.

While this screen is up, the session guard is armed, so any shutdown trigger,
however it arrives, releases the resources.
"""

import os
import re
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import CenterMiddle, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Static

from wisp.cli.screens.base import WispScreen
from wisp.cli.screens.modal import ConfirmDestroyScreen
from wisp.cli.screens.session_ended import (
    ExternalTeardownScreen,
    SessionEndedScreen,
)
from wisp.cli.screens.shutdown import confirmed_destroy, request_shutdown
from wisp.config.constants import WIREGUARD_CLIENT_CONF_PATH
from wisp.daemon.server import STALE_TUNNEL_SECONDS
from wisp.providers.base import get_provider_display_name
from wisp.session import ActiveSession, read_active_session
from wisp.utils import logger
from wisp.utils.operations import read_teardown_notice

# How long a freshly mounted view waits for the first handshake before deciding
# that a tunnel with no handshake data at all is pointing at a dead server.
# WireGuard's keepalive is 25s, so three minutes is already generous.
HANDSHAKE_GRACE_SECONDS: int = 180

HEALTH_LIVE: str = "live"
HEALTH_WAITING: str = "waiting"
HEALTH_LOCAL_DOWN: str = "local_down"
HEALTH_REMOTE_GONE: str = "remote_gone"


def health_state(response, elapsed: float) -> str:
    """Classify a daemon status reply into a tunnel-health state.

    Kept as a pure function so the decision is testable without a daemon: the
    screen only renders what this returns.

    Args:
        response: The daemon's :class:`~wisp.daemon.protocol.Response`.
        elapsed (float): Seconds since this view started watching. Used to give
            a brand-new tunnel time to produce its first handshake.

    Returns:
        str: One of :data:`HEALTH_LIVE`, :data:`HEALTH_WAITING`,
        :data:`HEALTH_LOCAL_DOWN`, or :data:`HEALTH_REMOTE_GONE`. Only
        ``HEALTH_REMOTE_GONE`` means the remote end is gone; a down local
        interface says nothing about the remote (``wisp disconnect`` is local).
    """
    if not response.ok:
        return HEALTH_LOCAL_DOWN

    age = getattr(response, "handshake_age_seconds", None)
    if age is None:
        # The interface is up but no peer reports a handshake: either the
        # tunnel was just established, or it is talking to nothing.
        if elapsed > HANDSHAKE_GRACE_SECONDS:
            return HEALTH_REMOTE_GONE
        return HEALTH_WAITING

    if age > STALE_TUNNEL_SECONDS:
        return HEALTH_REMOTE_GONE
    return HEALTH_LIVE


def _read_endpoint() -> str | None:
    """Return the ``Endpoint`` value from the stored client config, if any.

    An adopted session has no in-memory deployment result, so the client config
    is the only local record of where the tunnel points.

    Returns:
        str | None: ``host:port`` from the ``[Peer]`` section, or None when the
        file is missing or has no endpoint.
    """
    try:
        content = WIREGUARD_CLIENT_CONF_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*Endpoint\s*=\s*(\S+)\s*$", content, re.MULTILINE)
    return match.group(1) if match else None


class TunnelScreen(WispScreen):
    """Live view of the running tunnel, with its lifetime on screen."""

    BINDINGS = [
        Binding("escape", "destroy", "Destruir", show=True),
        Binding("d", "destroy", "Destruir", show=False),
    ]

    CSS = """
    #tunnel-title {
        text-align: center;
        margin-bottom: 0;
    }

    #tunnel-subtitle {
        text-align: center;
        margin-bottom: 0;
    }

    #tunnel-panel {
        height: auto;
        border: round #10b981;
        padding: 1 2;
        margin-top: 1;
        margin-bottom: 0;
    }

    #tunnel-panel .row {
        height: auto;
    }

    #tunnel-panel .label {
        width: 20;
        color: #94a3b8;
    }

    #tunnel-panel .value {
        width: 1fr;
    }

    #tunnel-health {
        text-align: center;
        margin-top: 0;
        margin-bottom: 0;
        height: auto;
    }

    #tunnel-note {
        text-align: center;
        color: #64748b;
        margin-top: 0;
    }

    #tunnel-buttons {
        height: 3;
        margin-top: 1;
    }

    #tunnel-buttons Button {
        width: 1fr;
    }
    """

    def __init__(self, adopted: ActiveSession | None = None) -> None:
        """Initialize the view.

        Args:
            adopted (ActiveSession | None): Marker of a tunnel recovered from a
                previous run. When given, the panel reads its identity from the
                marker and the client config instead of an in-memory deployment.
        """
        super().__init__()
        self._adopted = adopted
        self._ended = False
        self._external_teardown_seen = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card"):
                yield Static(
                    "[bold green]Active Tunnel[/bold green]",
                    id="tunnel-title",
                    classes="title",
                )
                yield Static(
                    "Tunnel remains active while Wisp is running.",
                    id="tunnel-subtitle",
                    classes="subtitle",
                )
                with Vertical(id="tunnel-panel"):
                    yield Static("", id="tunnel-session", classes="row")
                    yield Static("", id="tunnel-endpoint", classes="row")
                    yield Static("", id="tunnel-instance", classes="row")
                    yield Static("", id="tunnel-uptime", classes="row")
                yield Static("", id="tunnel-health")
                yield Static(
                    "q / Ctrl+C / Esc destroy the tunnel and all resources.",
                    id="tunnel-note",
                    classes="subtitle",
                )
                with Horizontal(id="tunnel-buttons", classes="btn-group"):
                    yield Button(
                        "Destroy Tunnel and Exit",
                        id="btn-tunnel-destroy",
                        variant="error",
                        classes="btn-danger",
                    )
        yield Footer()

    def on_mount(self) -> None:
        """Render the session details and start the uptime ticker."""
        self._started_at = time.time()
        self._refresh_details()
        if self._adopted is not None:
            self.query_one("#tunnel-subtitle", Static).update(
                "This tunnel was recovered from a previous run. "
                "Quitting now destroys it."
            )
        self._ticker = self.set_interval(1, self._tick)

    def on_unmount(self) -> None:
        """Stop the ticker when leaving the screen."""
        ticker = getattr(self, "_ticker", None)
        if ticker is not None:
            ticker.stop()
            self._ticker = None

    def _tick(self) -> None:
        """Refresh uptime, detect an external teardown, and re-check health.

        Once the session has ended there is nothing left to observe: the view
        has been replaced by an end screen, so polling would only spin.
        """
        if self._ended:
            return
        self._refresh_uptime()
        self._check_external_teardown()
        if self._ended:
            return
        state = self._refresh_health()
        if state == HEALTH_REMOTE_GONE:
            self._handle_remote_gone()

    @property
    def _deployment(self):
        """Last successful deployment recorded in the app state."""
        return getattr(self.app.state, "last_deployment", None)

    def _refresh_details(self) -> None:
        """Fill the panel with the session's identity and endpoint."""
        state = self.app.state
        deployment = self._deployment

        if deployment is None and self._adopted is not None:
            self._refresh_adopted_details()
            return

        if deployment is None:
            self.query_one("#tunnel-session", Static).update(
                "[yellow]No active deployment.[/yellow]"
            )
            self.query_one("#tunnel-endpoint", Static).update("")
            self.query_one("#tunnel-instance", Static).update("")
            return

        provider_display = get_provider_display_name(state.provider_name)
        self.query_one("#tunnel-session", Static).update(
            f"[dim]Provider:[/dim] {provider_display}"
            f"   [dim]Region:[/dim] {state.selected_region}"
        )
        self.query_one("#tunnel-endpoint", Static).update(
            f"[dim]Public IP:[/dim] [bold yellow]{deployment['public_ip']}[/bold yellow]"
            f"   [dim]WireGuard:[/dim] UDP {deployment['wireguard_port']}"
        )
        self.query_one("#tunnel-instance", Static).update(
            f"[dim]Instance:[/dim] {deployment['instance_id']}"
            f"   [dim]Private IP:[/dim] {deployment['private_ip']}"
        )
        self._refresh_uptime()

    def _refresh_adopted_details(self) -> None:
        """Fill the panel from the marker and the stored client config.

        An adopted session has no in-memory deployment result, so the public IP,
        instance id and private IP were never observed by this process. Inventing
        them would be worse than saying so; the endpoint is read from the client
        config because that is a real local record of where the tunnel points.
        """
        state = self.app.state
        provider_display = get_provider_display_name(state.provider_name)
        endpoint = _read_endpoint()
        self.query_one("#tunnel-session", Static).update(
            f"[dim]Provider:[/dim] {provider_display}"
            f"   [dim]Region:[/dim] {state.selected_region}"
        )
        self.query_one("#tunnel-endpoint", Static).update(
            f"[dim]Endpoint:[/dim] [bold yellow]"
            f"{endpoint or 'unknown (adopted session)'}[/bold yellow]"
        )
        self.query_one("#tunnel-instance", Static).update(
            "[dim]Instance:[/dim] unknown (adopted session)"
        )
        self._refresh_uptime()

    def _refresh_uptime(self) -> None:
        """Update the elapsed-time row."""
        session = read_active_session()
        if session is not None and session.started_at:
            elapsed = session.uptime_seconds
        else:
            elapsed = max(0.0, time.time() - self._started_at)
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        self.query_one("#tunnel-uptime", Static).update(
            f"[dim]Uptime:[/dim] {hours:d}h {minutes:02d}m {seconds:02d}s"
        )

    def _refresh_health(self) -> str:
        """Poll the local daemon and report whether wg0 is still up.

        Returns:
            str: The health state from :func:`health_state`, so the ticker can
            act on ``HEALTH_REMOTE_GONE`` without re-interpreting text.
        """
        try:
            from wisp.wireguard import status_wireguard_client

            response = status_wireguard_client()
        except Exception as exc:
            logger.debug(f"Tunnel health check failed: {exc}")
            self.query_one("#tunnel-health", Static).update(
                "[yellow]Local status: unknown (daemon unreachable)[/yellow]"
            )
            return HEALTH_LOCAL_DOWN

        state = health_state(response, time.time() - self._started_at)
        if state == HEALTH_LOCAL_DOWN:
            self.query_one("#tunnel-health", Static).update(
                "[bold yellow]● Local interface down[/bold yellow]"
                f" [dim]{response.message}[/dim]"
            )
        elif state == HEALTH_WAITING:
            self.query_one("#tunnel-health", Static).update(
                "[bold green]● Tunnel established[/bold green]"
                " [dim]waiting for the first handshake...[/dim]"
            )
        elif state == HEALTH_REMOTE_GONE:
            self.query_one("#tunnel-health", Static).update(
                "[bold red]● Remote end not responding[/bold red]"
                " [dim]no recent WireGuard handshake[/dim]"
            )
        else:
            self.query_one("#tunnel-health", Static).update(
                f"[bold green]● Tunnel established[/bold green]"
                f" [dim]{response.message or 'wg0 active'}[/dim]"
            )
        return state

    def _handle_remote_gone(self) -> None:
        """Stop pretending the session is alive once the remote end is gone.

        The local interface can stay up indefinitely pointing at a server that
        no longer exists. Leaving the guard armed would make ``q`` run a slow
        destroy of resources that are already gone, so the guard is detached and
        the user is told what actually happened.
        """
        if self._ended:
            return
        self._ended = True
        ticker = getattr(self, "_ticker", None)
        if ticker is not None:
            ticker.stop()
            self._ticker = None
        logger.info("Tunnel view detected that the remote end is gone")
        self.app.state.session_guard.disarm()
        self.app.switch_screen(SessionEndedScreen(session=read_active_session()))

    def _check_external_teardown(self) -> None:
        """Leave the live view when another process is destroying the session.

        A scheduled ``wisp destroy`` is a detached OS process with no channel
        back into this event loop, so the teardown notice file is the only
        signal available. It is written by whoever holds the destroy lock.
        """
        if self._external_teardown_seen:
            return
        notice = read_teardown_notice()
        if notice is None:
            return
        if notice.get("pid") == os.getpid():
            # Our own teardown, already reported by the shutdown screen.
            return

        self._external_teardown_seen = True
        self._ended = True
        ticker = getattr(self, "_ticker", None)
        if ticker is not None:
            ticker.stop()
            self._ticker = None
        # Another process owns the destroy. Fighting it for the operation lock
        # would only make this TUI wait and then report a second destroy.
        self.app.state.session_guard.disarm()
        logger.info("Another Wisp process is destroying this session")
        self.app.switch_screen(ExternalTeardownScreen(notice))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-tunnel-destroy":
            self.action_destroy()

    def action_destroy(self) -> None:
        """Destroy the tunnel and its resources, then exit.

        The shutdown screen owns the teardown so the user sees progress: a
        cloud destroy blocks for minutes and would otherwise look like a hang.
        Unless confirmations were disabled in ``[general]``, a modal asks first
        — a destroyed tunnel is the one thing on this screen that is
        irreversible (there is deliberately no way back to the menu).
        """
        state = self.app.state  # type: ignore[attr-defined]
        if state.config.confirm_destroy:
            self.app.push_screen(ConfirmDestroyScreen(), self._handle_destroy_choice)
        else:
            request_shutdown(self.app, "tunnel destroyed from the tunnel screen")

    def _handle_destroy_choice(self, choice: tuple[bool, bool] | None) -> None:
        """Apply the destroy modal's verdict: proceed, and maybe stop asking."""
        confirmed_destroy(
            self.app,
            choice,
            "tunnel destroyed from the tunnel screen",
        )
