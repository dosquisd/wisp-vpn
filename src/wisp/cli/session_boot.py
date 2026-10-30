"""Session-guard arming shared by fresh deploys and adopted tunnels.

A live tunnel has exactly one way out: destroying it. Every path that arms that
teardown must build it the same way, otherwise quitting behaves differently
depending on how the session was created. This module owns that construction so
the deploy flow and the startup adoption flow cannot drift apart.

Two switches exist because arming is not always the same operation:

- ``write_marker`` exists because adoption must NOT rewrite the marker. The
  marker records when the session started and when it expires; rewriting it
  would silently extend the session past its TTL.
- ``schedule_watchdog`` exists because adoption must not re-arm the OS one-shot.
  That timer is already scheduled for the original deadline; scheduling it again
  would push the deadline further out every time the TUI is reopened.
"""

from wisp.session import teardown_active_session, write_active_session
from wisp.utils.watchdog import schedule_ttl_watchdog


def arm_session_guard(
    app,
    *,
    provider: str,
    region: str,
    ttl_minutes: int,
    vps: dict | None = None,
    write_marker: bool = True,
    schedule_watchdog: bool = True,
) -> None:
    """Arm the session guard so any exit path releases the tunnel.

    The guard is idempotent: whichever trigger fires first performs the teardown
    and the rest are no-ops. That is what lets keybindings, POSIX signals,
    ``atexit`` and adoption all share one teardown.

    Args:
        app: The running :class:`~wisp.cli.app.WispApp`.
        provider (str): Provider name owning the session.
        region (str): Region the resources live in.
        ttl_minutes (int): Session TTL in minutes; ``0`` disables the OS one-shot.
        vps (dict | None): VPS connection details to record in the marker, so a
            later destroy does not depend on the TOML ``[vps]`` section.
        write_marker (bool): Write ``active-session.json``. False for adoption,
            where the existing marker must keep its original start and expiry.
        schedule_watchdog (bool): Schedule the OS-level TTL one-shot. False for
            adoption, where that timer is already armed for the original deadline.
    """
    if write_marker:
        write_active_session(
            provider,
            region,
            vps=vps,
            ttl_minutes=ttl_minutes,
        )

    # Optional OS-level TTL watchdog: fires `wisp destroy` even if this process
    # dies, so a forgotten session cannot bill forever. This is the local layer;
    # the in-VM watchdog armed during deploy is the remote one.
    if schedule_watchdog and ttl_minutes > 0:
        schedule_ttl_watchdog(provider, region, ttl_minutes)

    def teardown(reason: str) -> None:
        """Release the session's resources.

        Args:
            reason (str): Why the session is shutting down.
        """
        report = app.thread_safe_call
        report(f"Destroying tunnel ({reason})...")
        teardown_active_session(
            provider,
            region,
            on_progress=lambda msg, _p: report(msg),
        )

    app.state.session_guard.arm(teardown)
