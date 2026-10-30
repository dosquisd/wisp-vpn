from pulumi import automation as auto

from wisp.config.constants import PULUMI_PROJECT_NAME, get_pulumi_stack_name
from wisp.utils.logger import logger


def create_or_select_pulumi_stack(
    program: auto.PulumiFn | None = None,
    *,
    provider: str = "aws",
    project_name: str = PULUMI_PROJECT_NAME,
) -> auto.Stack:
    """Create or select the Pulumi stack via the Automation API.

    Args:
        program (auto.PulumiFn | None): The inline Pulumi program to run.
        provider (str): Cloud provider name (e.g., "aws", "oci"). Used to generate
            provider-specific stack name.
        project_name (str): Project name (defaults to ``wisp-project``).

    Returns:
        auto.Stack: The created or selected stack.
    """
    stack_name = get_pulumi_stack_name(provider)
    return auto.create_or_select_stack(
        stack_name=stack_name, project_name=project_name, program=program
    )


def list_wisp_stacks() -> list[str]:
    """List every Pulumi stack that belongs to Wisp in the current backend.

    Args:
        None.

    Returns:
        list[str]: Stack names (e.g. ``["wisp-stack-aws", ...]``), sorted.
    """
    workspace = auto.LocalWorkspace(
        project_settings=auto.ProjectSettings(
            name=PULUMI_PROJECT_NAME, runtime="python"
        )
    )
    return sorted(
        info.name
        for info in workspace.list_stacks()
        if info.name.startswith("wisp-stack-")
    )


def stack_resource_counts(stack_name: str) -> dict[str, int]:
    """Count the real (billable-affecting) resources recorded in a stack state.

    Reads the stack's exported state without running any program, so this is
    safe against stacks whose program has drifted or whose provider credentials
    are currently broken.

    Args:
        stack_name (str): Pulumi stack name (e.g. ``"wisp-stack-aws"``).

    Returns:
        dict[str, int]: ``{"total": <n>}`` where ``n`` counts every resource
        except the synthetic ``pulumi:pulumi:Stack`` root.
    """
    workspace = auto.LocalWorkspace(
        project_settings=auto.ProjectSettings(
            name=PULUMI_PROJECT_NAME, runtime="python"
        )
    )
    deployment = workspace.export_stack(stack_name).deployment
    resources = deployment.get("resources", []) or []
    real = [r for r in resources if r.get("type") != "pulumi:pulumi:Stack"]
    return {"total": len(real)}


def refresh_wisp_stack(program: auto.PulumiFn, *, provider: str, region: str) -> bool:
    """Reconcile a stack's state against reality (``stack.refresh``).

    Resources deleted out-of-band (e.g. a VM removed by hand in the cloud
    console) stay recorded in the state until a refresh marks them deleted.
    Refreshing before counting/destroying keeps both truthful.

    Note: the provider's program factory is injected by the caller instead of
    looked up here — this module must not import ``wisp.providers``, because
    the provider modules themselves import ``create_or_select_pulumi_stack``
    from ``wisp.utils`` (lazy attribute), which would close a cycle.

    Args:
        program (auto.PulumiFn): The provider's program factory
            (``_create_pulumi_program``); called with ``(region,)``.
        provider (str): Provider identifier owning the stack (``"aws"``,
            ``"oci"``, ``"gcp"``); used for the stack name.
        region (str): Region the stack was deployed in (program input).

    Returns:
        bool: True when the refresh completed, False when it failed (e.g.
        broken credentials); never raises.
    """
    try:
        stack = create_or_select_pulumi_stack(
            lambda: program(region), provider=provider
        )
        stack.refresh()
        return True
    except Exception as exc:
        logger.debug(f"Refresh skipped for wisp-stack-{provider}: {exc}")
        return False


def destroy_wisp_stack(program: auto.PulumiFn, *, provider: str, region: str) -> int:
    """Destroy a wisp stack's resources through the normal provider flow.

    Reuses :func:`create_or_select_pulumi_stack` with the provider's program
    factory so the destroy behaves exactly like an in-app teardown. The
    program is injected by the caller (see the cycle note in
    :func:`refresh_wisp_stack`).

    Args:
        program (auto.PulumiFn): The provider's program factory
            (``_create_pulumi_program``); called with ``(region,)``.
        provider (str): Provider identifier owning the stack (``"aws"``,
            ``"oci"``, ``"gcp"``); used for the stack name.
        region (str): Region the stack was deployed in (program input).

    Returns:
        int: Number of deleted resources.
    """
    stack = create_or_select_pulumi_stack(lambda: program(region), provider=provider)
    result = stack.destroy()
    return result.summary.resource_changes.get("delete", 0)


def describe_pulumi_error(exc: Exception) -> str:
    """Turn a Pulumi Automation exception into text a user can act on.

    Pulumi already serializes stack updates: a second concurrent update fails
    with :class:`~pulumi.automation.errors.ConcurrentUpdateError` rather than
    corrupting state. That case is worth naming explicitly, because the raw
    message reads like a defect instead of "wait a moment".

    For every other failure, the real diagnostics live in the wrapped
    ``command_result`` (stderr/stdout); ``str(exc)`` alone is usually just
    ``"code: 1"``, which tells the user nothing.

    Args:
        exc (Exception): The exception raised by a deploy, refresh or destroy.

    Returns:
        str: A multi-line, human-readable description of the failure.
    """
    if isinstance(exc, auto.errors.ConcurrentUpdateError):
        return (
            "Another Wisp operation is currently updating this stack, and Pulumi "
            "locked it for that update.\n"
            "Wait for it to finish (a scheduled `wisp destroy` is the usual "
            "cause) and try again."
        )

    command_result = getattr(exc, "command_result", None)
    if command_result is not None:
        text = f"{command_result.stderr or ''}\n{command_result.stdout or ''}"
        lines = [line for line in text.splitlines() if line.strip()]
        error_lines = [line for line in lines if "error" in line.lower()]
        detail = "\n".join(error_lines[:10]) if error_lines else "\n".join(lines[-10:])
        if detail.strip():
            return detail.rstrip()
    return str(exc)
