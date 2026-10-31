"""Wisp entry point.

Parses command-line arguments and dispatches to either the interactive TUI (no
subcommand) or a non-interactive command (``deploy``, ``destroy``, ``regions``,
``gc``, ``daemon``). Exposed as the ``wisp`` console script.
"""

import argparse
import enum
import json
import logging
import sys
from typing import TypedDict

from wisp.cli.app import WispApp
from wisp.config.constants import WIREGUARD_CLIENT_CONF_PATH, WIREGUARD_KEY_PATH
from wisp.config.credentials import (
    resolve_aws_credentials,
    resolve_gcp_credentials,
    resolve_oci_credentials,
    resolve_vps_credentials,
)
from wisp.config.settings import (
    WISP_USER_CONFIG_PATH,
    ensure_user_config,
    get_default_provider,
    get_default_region_for,
    load_wisp_config,
)
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.session import (
    clear_active_session,
    read_active_session,
    write_active_session,
)
from wisp.utils.logger import logger
from wisp.utils.operations import begin_teardown, end_teardown
from wisp.utils.pulumi import (
    describe_pulumi_error,
    destroy_wisp_stack,
    list_wisp_stacks,
    refresh_wisp_stack,
    stack_resource_counts,
)
from wisp.utils.watchdog import cancel_ttl_watchdog, schedule_ttl_watchdog
from wisp.wireguard import (
    connect_wireguard_client,
    disconnect_wireguard_client,
    tunnel_conflict,
)


class CommandEnum(enum.StrEnum):
    """Top-level commands accepted by the CLI."""

    TUI = "tui"
    DEPLOY = "deploy"
    DESTROY = "destroy"
    REGIONS = "regions"
    GC = "gc"
    DAEMON = "daemon"
    DISCONNECT = "disconnect"  # panic option: disconnect the local WireGuard client without destroying the remote VM
    CONNECT = "connect"


class RuntimeArgs(TypedDict):
    """Normalized runtime arguments produced by :func:`parse_args`."""

    command: CommandEnum
    provider: ProviderEnum | None
    region: str | None
    gc_destroy: bool
    gc_refresh: bool
    daemon_command: str | None
    ttl_minutes: int | None
    wireguard_port: int | None
    host: str | None
    ssh_user: str | None
    ssh_port: int | None
    ssh_key_path: str | None
    log_level: str | None


def parse_args(argv: list[str] | None = None) -> RuntimeArgs:
    """Parse CLI arguments into normalized :class:`RuntimeArgs`.

    Builds subparsers for ``deploy``/``destroy``/``regions`` (each with an
    optional ``provider`` positional; ``deploy``/``destroy`` also take
    ``-r/--region``), ``gc``, and ``daemon`` (with nested required
    ``install``/``uninstall`` subcommands). Command, provider, and region are
    lowercased; if no subcommand is given, the command defaults to the TUI.

    Args:
        argv (list[str] | None): Argument list to parse; ``None`` (the
            default) parses ``sys.argv``.
    """
    parser = argparse.ArgumentParser(description="Wisp CLI")

    # Parser for provider argument, which is common to all commands
    provider_parser = argparse.ArgumentParser(add_help=False)
    default_provider = get_default_provider()
    provider_parser.add_argument(
        "provider",
        nargs="?",
        type=str,
        choices=[
            provider.value
            for provider in ProviderEnum
            if provider != ProviderEnum.UNKNOWN
        ],
        default=default_provider,
        help="Cloud provider to use (default %(default)s).",
    )

    # Subparsers for different commands
    subparsers = parser.add_subparsers(dest="command")

    # Console log level for the CLI commands that stream provider activity.
    # The file log stays at DEBUG regardless.
    log_level_parser = argparse.ArgumentParser(add_help=False)
    log_level_parser.add_argument(
        "--log-level",
        type=str.lower,
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Console log verbosity (default: %(default)s). The rotating "
        "file log always keeps DEBUG.",
    )

    deploy_parser = subparsers.add_parser(
        "deploy",
        parents=[provider_parser, log_level_parser],
        help="Deploy a VM",
    )
    deploy_parser.add_argument(
        "-r",
        "--region",
        type=str,
        default=None,
        help="Region where the VM will be deployed "
        "(defaults to the provider's configured region).",
    )
    deploy_parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Session TTL watchdog: run `wisp destroy` automatically after "
        "this many minutes, even if Wisp exits (default: the configured "
        "[general].max_session_ttl_minutes; 0 disables the watchdog).",
    )
    deploy_parser.add_argument(
        "--wireguard-port",
        type=int,
        default=None,
        metavar="PORT",
        help="UDP port for the WireGuard tunnel (default: the configured "
        "[general].wireguard_port; 0 picks a random port).",
    )
    deploy_parser.add_argument(
        "--host",
        type=str,
        default=None,
        metavar="HOST",
        help="VPS only: the server's public hostname or IP (overrides [vps].host).",
    )
    deploy_parser.add_argument(
        "--ssh-user",
        type=str,
        default=None,
        metavar="USER",
        help="VPS only: SSH user to connect as (overrides [vps].ssh_user).",
    )
    deploy_parser.add_argument(
        "--ssh-port",
        type=int,
        default=None,
        metavar="PORT",
        help="VPS only: SSH port (overrides [vps].ssh_port).",
    )
    deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        metavar="PATH",
        help="VPS only: path to your SSH private key (overrides [vps].ssh_key_path).",
    )

    destroy_parser = subparsers.add_parser(
        "destroy",
        parents=[provider_parser, log_level_parser],
        help="Destroy VMs",
    )
    destroy_parser.add_argument(
        "-r",
        "--region",
        type=str,
        default=None,
        help="Region from which VMs will be destroyed "
        "(defaults to the provider's configured region).",
    )
    destroy_parser.add_argument(
        "--host",
        type=str,
        default=None,
        metavar="HOST",
        help="VPS only: the server's hostname/IP (overrides the deploy-time "
        "marker and [vps].host).",
    )
    destroy_parser.add_argument(
        "--ssh-user",
        type=str,
        default=None,
        metavar="USER",
        help="VPS only: SSH user (overrides the marker and [vps].ssh_user).",
    )
    destroy_parser.add_argument(
        "--ssh-port",
        type=int,
        default=None,
        metavar="PORT",
        help="VPS only: SSH port (overrides the marker and [vps].ssh_port).",
    )
    destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        metavar="PATH",
        help="VPS only: SSH private key path (overrides the marker and "
        "[vps].ssh_key_path).",
    )

    subparsers.add_parser(
        "regions",
        parents=[provider_parser],
        help="List available regions",
    )

    gc_parser = subparsers.add_parser(
        "gc",
        help="Inspect leftover Wisp Pulumi stacks and optionally destroy them",
    )
    gc_parser.add_argument(
        "--destroy",
        action="store_true",
        help="Destroy every Wisp stack that still holds resources "
        "(default: report only).",
    )
    gc_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh each stack's state against the cloud before reporting, "
        "so resources deleted out-of-band (e.g. a VM removed by hand in the "
        "console) stop counting as leftovers.",
    )

    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Install or remove the privileged local daemon",
        parents=[log_level_parser],
    )
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command", required=True
    )
    daemon_subparsers.add_parser(
        "install",
        help="Install (or refresh) the privileged local daemon",
    )
    daemon_subparsers.add_parser(
        "uninstall",
        help="Remove the privileged local daemon",
    )

    subparsers.add_parser(
        "connect",
        help="Connect the local WireGuard client to the remote VM",
    )

    subparsers.add_parser(
        "disconnect",
        help="Disconnect the local WireGuard client from the remote VM",
    )

    args = parser.parse_args(argv).__dict__

    return RuntimeArgs(
        command=(
            args["command"].lower()
            if args.get("command") is not None
            else CommandEnum.TUI
        ),
        provider=(
            ProviderEnum(args["provider"].lower())
            if args.get("provider") is not None
            else None
        ),
        region=args["region"].lower() if args.get("region") is not None else None,
        gc_destroy=bool(args.get("destroy", False)),
        gc_refresh=bool(args.get("refresh", False)),
        daemon_command=args.get("daemon_command"),
        ttl_minutes=args.get("ttl"),
        wireguard_port=args.get("wireguard_port"),
        host=args.get("host"),
        ssh_user=args.get("ssh_user"),
        ssh_port=args.get("ssh_port"),
        ssh_key_path=args.get("ssh_key_path"),
        log_level=args.get("log_level"),
    )


def _run_gc(destroy: bool = False, refresh: bool = False) -> None:
    """Report (and optionally destroy) leftover Wisp Pulumi stacks.

    Reads each stack's exported state without running any program, so a stack
    with broken credentials or drifted program still reports its resources.
    With ``--refresh``, each stack is reconciled against the cloud first, so
    resources deleted out-of-band stop counting as leftovers. With
    ``--destroy``, every stack holding real resources goes through the normal
    provider destroy flow (after a best-effort refresh).

    Args:
        destroy (bool): When True, destroy stacks that still hold resources.
        refresh (bool): When True, reconcile each stack's state first.
    """
    stacks = list_wisp_stacks()
    if not stacks:
        print("No Wisp Pulumi stacks found. Nothing to collect.")
        return

    leftover: dict[str, tuple[str, int]] = {}
    for stack_name in stacks:
        try:
            count = stack_resource_counts(stack_name)["total"]
        except Exception as exc:
            print(f"{stack_name}: could not read state ({exc})")
            continue
        provider = stack_name.removeprefix("wisp-stack-")
        region = (
            get_default_region_for(provider) or ""
        )  # Best available guess; deletion is state-driven.
        if refresh and count > 0:
            print(f"{stack_name}: refreshing state against the cloud...")
            if refresh_wisp_stack(
                _program_for(provider), provider=provider, region=region
            ):
                try:
                    count = stack_resource_counts(stack_name)["total"]
                except Exception as exc:
                    print(f"{stack_name}: could not re-read state ({exc})")
                    continue
        if count > 0:
            leftover[provider] = (region or "unknown", count)
        print(f"{stack_name}: {count} resource(s)")

    if not leftover:
        print("All Wisp stacks are empty. Nothing to collect.")
        return

    print(
        "\nStacks still holding resources: "
        + json.dumps({p: c for p, (_, c) in leftover.items()}, indent=2)
    )
    if not destroy:
        print("Run `wisp gc --destroy` to tear them down.")
        return

    # No mutual-exclusion lock here: Pulumi's backend already locks a stack for
    # the duration of an update, so a concurrent deploy or destroy fails with a
    # clear ConcurrentUpdateError instead of corrupting state.
    #
    # Disconnect the local WireGuard client before destroying any stacks, so the
    # daemon can remove the local artifacts (SSH key + client conf) without
    # leaving the client in a broken state. The daemon will also remove the
    # server-side configs on the remote VM.
    print("\nDisconnecting local WireGuard client...")
    disconnect_response = disconnect_wireguard_client()
    if not disconnect_response.ok:
        logger.warning(
            f"WireGuard client disconnect failed (may not be connected): {disconnect_response.message}."
        )

    destroyed_any = False
    for provider, (region, count) in leftover.items():
        print(f"\nDestroying {count} resource(s) from wisp-stack-{provider}...")
        begin_teardown(provider, region)
        try:
            # Best-effort refresh first: destroying ghost resources (deleted
            # out-of-band) would otherwise fail per resource.
            refresh_wisp_stack(_program_for(provider), provider=provider, region=region)
            deleted = destroy_wisp_stack(
                _program_for(provider), provider=provider, region=region
            )
            destroyed_any = True
            print(f"wisp-stack-{provider}: deleted {deleted} resource(s)")
        except Exception as exc:
            logger.debug(f"gc destroy failed for wisp-stack-{provider}", exc_info=True)
            print(
                f"wisp-stack-{provider}: destroy failed:\n{describe_pulumi_error(exc)}"
            )
        finally:
            end_teardown()

    if destroyed_any:
        # The destroyed stacks' local WireGuard artifacts (SSH key + client
        # conf) are orphans now — the same cleanup a normal teardown runs.
        # Skipped while a session is active: those files may belong to it.
        active = read_active_session()
        if active is not None:
            print(f"Local artifacts left alone: session {active.summary()} is active.")
            return
        removed = []
        for artifact in (WIREGUARD_KEY_PATH, WIREGUARD_CLIENT_CONF_PATH):
            try:
                if artifact.exists():
                    artifact.unlink()
                    removed.append(str(artifact))
            except OSError as exc:
                print(f"Could not remove {artifact}: {exc}")
        if removed:
            print("Removed local artifacts:")
            for path in removed:
                print(f"  - {path}")


def _program_for(provider: str):
    """Return the provider's Pulumi program factory for gc operations.

    Args:
        provider (str): Provider identifier (``"aws"``, ``"oci"``, ``"gcp"``).

    Returns:
        Callable: ``_create_pulumi_program`` bound to the provider class.
    """
    provider_cls = PROVIDERS_MAP[ProviderEnum(provider)]
    return provider_cls()._create_pulumi_program  # pyright: ignore[reportAttributeAccessIssue] # noqa: SLF001


def _create_provider_with_credentials(provider_option: ProviderEnum):
    """Create provider instance with resolved credentials."""
    if provider_option == ProviderEnum.AWS:
        creds = resolve_aws_credentials()
        return PROVIDERS_MAP[provider_option](credentials=creds)
    elif provider_option == ProviderEnum.OCI:
        creds = resolve_oci_credentials()
        return PROVIDERS_MAP[provider_option](credentials=creds)
    elif provider_option == ProviderEnum.GCP:
        creds = resolve_gcp_credentials()
        return PROVIDERS_MAP[provider_option](credentials=creds)
    elif provider_option == ProviderEnum.VPS:
        creds = resolve_vps_credentials()
        return PROVIDERS_MAP[provider_option](credentials=creds)
    else:
        return PROVIDERS_MAP[provider_option]()


def main() -> None:
    """Run Wisp: launch the TUI, or execute a non-interactive command.

    With no subcommand, starts the Textual TUI. Otherwise runs the selected
    non-interactive command (``gc``, ``daemon`` install/uninstall, ``regions``,
    ``deploy``, or ``destroy``), printing the result and exiting.
    """
    args = parse_args()
    command = args["command"]
    region = args["region"]
    provider_option = args["provider"]

    # Self-heal the single config file: a fresh install (or a wiped config
    # directory) would otherwise run on pure defaults with no hint of where
    # settings belong. Explicit call at the entry points — never an import-time
    # side effect.
    ensure_user_config()

    if command == CommandEnum.GC:
        _run_gc(destroy=args["gc_destroy"], refresh=args["gc_refresh"])
        sys.exit(0)

    if command == CommandEnum.TUI:
        app = WispApp()
        app.run()
        sys.exit(0)

    # Console verbosity: --log-level (default INFO). The rotating file log
    # always keeps DEBUG, so postmortems never lose detail.
    level_name = (args["log_level"] or "info").upper()
    logger.handlers[1].setLevel(getattr(logging, level_name, logging.INFO))

    if command == CommandEnum.DAEMON:
        # Lazy import: importing wisp.daemon.install at module level would set
        # WISP_DAEMON=1 in this process (wisp/daemon/__init__.py) and flip the
        # CLI logger to the daemon's stderr-only DEBUG sink for every command.
        from wisp.daemon.install import install_daemon, uninstall_daemon

        if args["daemon_command"] == "install":
            sys.exit(install_daemon())
        sys.exit(uninstall_daemon())

    if command == CommandEnum.CONNECT:
        if not WIREGUARD_CLIENT_CONF_PATH.exists():
            print(
                f"WireGuard client config not found at {WIREGUARD_CLIENT_CONF_PATH}. "
                "Please deploy a VM first.",
                file=sys.stderr,
            )
            sys.exit(1)

        content = WIREGUARD_CLIENT_CONF_PATH.read_text()
        connect_response = connect_wireguard_client(content)
        if connect_response.ok:
            print("WireGuard client connected successfully.")
            sys.exit(0)
        else:
            print(
                f"Failed to connect WireGuard client: {connect_response.message}",
                file=sys.stderr,
            )
            sys.exit(1)

    if command == CommandEnum.DISCONNECT:
        disconnect_response = disconnect_wireguard_client()
        if disconnect_response.ok:
            print("WireGuard client disconnected successfully.")
            sys.exit(0)
        else:
            print(
                f"Failed to disconnect WireGuard client: {disconnect_response.message}",
                file=sys.stderr,
            )
            sys.exit(1)

    # It's not necessary to do this assertion, because the CLI will always provide a provider, but it's a good safety check
    assert provider_option is not None, (
        "Provider must be specified for deploy, destroy, or regions commands."
    )
    provider = _create_provider_with_credentials(provider_option)

    if command == CommandEnum.REGIONS:
        logger.info(f"Fetching available regions for provider: {provider_option.value}")
        regions = provider.get_available_regions()
        print(f"Available regions:\n{json.dumps(regions, indent=2)}")
        sys.exit(0)

    if region is None:
        region = getattr(provider.credentials, "region", None)

    # The region is optional for the VPS provider (nothing to provision), but
    # the cloud providers must refuse to deploy without one: an empty string
    # used to sail through to Pulumi and fail there with a cryptic provider
    # error, leaving pending operations stuck in the stack state.
    if provider_option != ProviderEnum.VPS and not region:
        print(
            f"No region configured for {provider_option.value}. Pass -r/--region, "
            f"or set it under the [{provider_option.value}] section of "
            f"{WISP_USER_CONFIG_PATH}.",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info(
        f"Running in CLI mode with command: {command}. "
        f"Provider: {provider_option.value}, Region: {region}"
    )

    if command == CommandEnum.DEPLOY:
        config = load_wisp_config()
        if args["wireguard_port"] is not None:
            config.wireguard_port = max(0, int(args["wireguard_port"]))

        # --ttl wins over the configured default (0 keeps the watchdog off).
        # Providers read the session TTL from ``config`` to arm the in-VM
        # watchdog, so resolve it before provisioning, not after.
        ttl_minutes = (
            args["ttl_minutes"]
            if args["ttl_minutes"] is not None
            else config.max_session_ttl_minutes
        )
        config.max_session_ttl_minutes = ttl_minutes

        vps_payload: dict | None = None
        if provider_option == ProviderEnum.VPS:
            # CLI flags override the TOML [vps] section for this deployment.
            creds = resolve_vps_credentials()
            if args["host"]:
                creds.host = args["host"]
            if args["ssh_user"]:
                creds.ssh_user = args["ssh_user"]
            if args["ssh_port"] is not None:
                creds.ssh_port = int(args["ssh_port"])
            if args["ssh_key_path"]:
                creds.ssh_key_path = args["ssh_key_path"]
            provider = PROVIDERS_MAP[provider_option](credentials=creds)
            vps_payload = {
                "host": creds.host,
                "ssh_user": creds.ssh_user,
                "ssh_port": creds.ssh_port,
                "ssh_key_path": creds.ssh_key_path,
            }

        # Refuse before provisioning: a local name conflict is predictable, and
        # discovering it after `deploy_vm` would leave a billable VM behind.
        conflict = tunnel_conflict()
        if conflict:
            print(conflict, file=sys.stderr)
            print(
                "No VM was deployed. Wisp only manages the WireGuard tunnel it "
                "created.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            vm_result = provider.deploy_vm(region=region, config=config)
        except Exception as exc:
            logger.debug("CLI deploy failed", exc_info=True)
            print(f"Deployment failed:\n{describe_pulumi_error(exc)}", file=sys.stderr)
            sys.exit(1)

        print(f"Deployed VM:\n{json.dumps(vm_result, indent=2)}")

        # Record the session even though this process is about to exit: the
        # marker is what lets a later TUI (or `wisp destroy`) know a tunnel is
        # still out there, and its TTL fields decide whether that session can be
        # resumed instead of destroyed.
        write_active_session(
            provider_option.value,
            region,
            vps=vps_payload,
            ttl_minutes=ttl_minutes,
        )

        # Optional OS-level TTL watchdog: fires `wisp destroy` even after this
        # process exits, so a forgotten CLI session cannot bill forever. This is
        # the local layer; the in-VM watchdog armed during deploy is the remote
        # one and survives this machine shutting down.
        if ttl_minutes and ttl_minutes > 0:
            schedule_ttl_watchdog(provider_option.value, region, ttl_minutes)
        sys.exit(0)

    if command == CommandEnum.DESTROY:
        begin_teardown(provider_option.value, region or "")
        try:
            # Cancel the optional TTL watchdog before/after teardown: best effort,
            # and harmless if it already fired or was never scheduled.
            cancel_ttl_watchdog(provider_option.value, region)
            # VPS: the deploy-time connection details live in the marker (the TOML
            # may never have had them for flag-based deploys). Precedence:
            # CLI flags > deploy-time marker > TOML [vps].
            if provider_option == ProviderEnum.VPS:
                marker = read_active_session()
                marker_vps = marker.vps if marker is not None else {}
                creds = resolve_vps_credentials()
                creds.host = args["host"] or marker_vps.get("host") or creds.host
                creds.ssh_user = (
                    args["ssh_user"] or marker_vps.get("ssh_user") or creds.ssh_user
                )
                creds.ssh_port = (
                    args["ssh_port"] or marker_vps.get("ssh_port") or creds.ssh_port
                )
                creds.ssh_key_path = (
                    args["ssh_key_path"]
                    or marker_vps.get("ssh_key_path")
                    or creds.ssh_key_path
                )
                provider = PROVIDERS_MAP[provider_option](credentials=creds)

            try:
                delete_success = provider.delete_vm(region=region)
            except Exception as exc:
                # Pulumi's own stack lock produces the "another operation is in
                # progress" case; everything else is unwrapped from its output.
                logger.debug("CLI destroy failed", exc_info=True)
                print(
                    f"Destroy failed:\n{describe_pulumi_error(exc)}",
                    file=sys.stderr,
                )
                sys.exit(1)

            # If this destroy matches a session the TUI marked as active, clear the
            # marker: resources are gone, and leaving it would make the next TUI
            # startup offer to "clean up" an orphan that no longer exists.
            active = read_active_session()
            if (
                active is not None
                and active.provider == provider_option.value
                and active.region == region
            ):
                clear_active_session()
            if provider_option == ProviderEnum.VPS:
                # Nothing is deleted on the VPS: the tunnel is torn down on both
                # ends and Wisp's own configs are removed.
                print(
                    "VPN tunnel removed (client disconnected, server configs cleaned)."
                )
            else:
                print(f"Deleted VM. Count: {delete_success}")
        finally:
            end_teardown()
        sys.exit(0)


if __name__ == "__main__":
    main()
