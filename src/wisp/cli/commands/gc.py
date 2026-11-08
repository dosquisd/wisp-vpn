"""Garbage collection CLI command handler."""

import argparse
import json

from wisp.cli.args import RuntimeArgs
from wisp.config.constants import WIREGUARD_CLIENT_CONF_PATH, WIREGUARD_KEY_PATH
from wisp.config.settings import get_default_region_for
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.session import read_active_session
from wisp.utils.logger import logger
from wisp.utils.operations import begin_teardown, end_teardown
from wisp.utils.pulumi import (
    describe_pulumi_error,
    destroy_wisp_stack,
    list_wisp_stacks,
    refresh_wisp_stack,
    stack_resource_counts,
)
from wisp.wireguard import disconnect_wireguard_client


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the gc command subparser.

    Args:
        subparsers: Subparsers action from the top-level argument parser.
    """
    gc_parser = subparsers.add_parser(
        "gc",
        help="Inspect leftover Wisp Pulumi stacks and optionally destroy them",
        description=(
            "Garbage collection for leftover Pulumi stacks. Lists every "
            "'wisp-stack-*' in the backend and counts the resources recorded "
            "in each stack's exported state. No program runs, so a stack with "
            "broken credentials or drifted code still reports its resources.\n\n"
            "This is the recovery path for infrastructure left behind by a "
            "deploy that died before its teardown ran."
        ),
        epilog=(
            "Examples:\n"
            "  wisp gc                 # report leftovers only\n"
            "  wisp gc --refresh       # reconcile state with the cloud first\n"
            "  wisp gc --destroy       # tear down every leftover stack\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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


def _program_for(provider: str):
    """Return the provider's Pulumi program factory for gc operations.

    Args:
        provider (str): Provider identifier (``"aws"``, ``"oci"``, ``"gcp"``).

    Returns:
        Callable: ``_create_pulumi_program`` bound to the provider class.
    """
    provider_cls = PROVIDERS_MAP[ProviderEnum(provider)]
    return provider_cls()._create_pulumi_program  # pyright: ignore[reportAttributeAccessIssue] # noqa: SLF001


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


def run(args: RuntimeArgs) -> int:
    """Execute the gc command.

    Args:
        args: Runtime arguments parsed from the CLI.

    Returns:
        int: Process exit code.
    """
    _run_gc(destroy=args["gc_destroy"], refresh=args["gc_refresh"])
    return 0
