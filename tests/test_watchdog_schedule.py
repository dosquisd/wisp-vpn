"""The local TTL watchdog must schedule a `wisp destroy` the CLI actually runs.

The scheduled argv is never executed until the timer fires, so a normal test run
proves nothing about it: the first real execution happens on the user's machine,
minutes later, inside a transient unit whose only trace is a failed
`systemctl status`. These tests parse the generated argv with the real CLI
parser, and pin the cancellation call: the watchdog's own `wisp destroy` must
not ask systemd to stop the service it is running inside.
"""

import ast
import shutil
import subprocess
import sys

import pytest

from wisp.main import CommandEnum, ProviderEnum, parse_args
from wisp.utils import watchdog as watchdog_module
from wisp.utils.platform import PlatformEnum
from wisp.utils.pulumi import describe_pulumi_error
from wisp.utils.watchdog import (
    _schedule_systemd,
    _watchdog_env,
    _wisp_destroy_argv,
    _wisp_destroy_command,
    cancel_ttl_watchdog,
    get_watchdog_task_name,
)

PROVIDER = "gcp"
REGION = "us-central1"


def test_scheduled_argv_is_accepted_by_the_real_cli_parser() -> None:
    """The argv the timer runs must parse; a positional region does not."""
    args = parse_args(_wisp_destroy_argv(PROVIDER, REGION))

    assert args["command"] == CommandEnum.DESTROY
    assert args["provider"] == ProviderEnum.GCP
    assert args["region"] == REGION


def test_region_is_not_a_positional_destroy_argument() -> None:
    """Pin the contract the watchdog got wrong: destroy takes `-r`, not a spare."""
    with pytest.raises(SystemExit):
        parse_args(["destroy", PROVIDER, REGION])


def test_command_prefers_the_console_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/wisp")

    command = _wisp_destroy_command(PROVIDER, REGION)

    assert command == ["/usr/local/bin/wisp", *_wisp_destroy_argv(PROVIDER, REGION)]


def test_interpreter_fallback_seeds_the_same_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a console script on PATH, sys.argv must carry the identical args."""
    monkeypatch.setattr(shutil, "which", lambda _: None)

    command = _wisp_destroy_command(PROVIDER, REGION)

    assert command[:2] == [sys.executable, "-c"]
    assignment = next(
        node
        for node in ast.parse(command[2]).body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "argv"
    )
    seeded = ast.literal_eval(assignment.value)
    assert seeded == ["wisp", *_wisp_destroy_argv(PROVIDER, REGION)]


def _record_subprocess_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture every argv handed to subprocess.run inside the watchdog module."""
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return None

    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        watchdog_module.PlatformEnum, "get_platform", lambda: PlatformEnum.LINUX
    )
    return calls


def test_cancel_stops_only_the_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping the service would SIGTERM the watchdog's own teardown."""
    calls = _record_subprocess_calls(monkeypatch)
    task_name = get_watchdog_task_name(PROVIDER, REGION)

    cancel_ttl_watchdog(PROVIDER, REGION)

    stop_calls = [argv for argv in calls if "stop" in argv]
    assert stop_calls == [["systemctl", "--user", "stop", f"{task_name}.timer"]]
    assert all(f"{task_name}.service" not in argv for argv in stop_calls)


def test_cancel_unloads_both_units_after_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """systemd-run reuses the unit name, so dead units must be reset."""
    calls = _record_subprocess_calls(monkeypatch)
    task_name = get_watchdog_task_name(PROVIDER, REGION)

    cancel_ttl_watchdog(PROVIDER, REGION)

    reset_calls = [argv for argv in calls if "reset-failed" in argv]
    assert reset_calls == [
        [
            "systemctl",
            "--user",
            "reset-failed",
            f"{task_name}.timer",
            f"{task_name}.service",
        ]
    ]


def test_watchdog_env_forwards_path_and_pulumi_variables() -> None:
    """The unit gets the user manager env, so PATH must be handed over."""
    env = _watchdog_env(
        {
            "PATH": "/home/u/.pulumi/bin:/usr/bin",
            "PULUMI_CONFIG_PASSPHRASE": "secret",
            "PULUMI_HOME": "",
            "UNRELATED": "ignored",
        }
    )

    assert env == {
        "PATH": "/home/u/.pulumi/bin:/usr/bin",
        "PULUMI_CONFIG_PASSPHRASE": "secret",
    }


def test_scheduled_unit_receives_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`systemd-run` must be told the PATH, or `pulumi` is never found."""
    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(
        watchdog_module,
        "_watchdog_env",
        lambda: {"PATH": "/home/u/.pulumi/bin:/usr/bin"},
    )
    monkeypatch.setattr(watchdog_module.subprocess, "run", fake_run)

    _schedule_systemd("wisp-ttl-gcp-us-central1", ["wisp", "destroy"], 1)

    argv = calls[0]
    assert "--setenv=PATH=/home/u/.pulumi/bin:/usr/bin" in argv
    assert argv.index("--") < argv.index("wisp")


def test_missing_pulumi_is_reported_as_an_environment_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[Errno 2] No such file or directory: 'pulumi'` alone hides the cause."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    message = describe_pulumi_error(FileNotFoundError(2, "No such file", "pulumi"))

    assert "Pulumi CLI could not be found" in message
    assert "PATH was: /usr/bin:/bin" in message
