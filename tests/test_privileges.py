"""Tests for the privileged-command hints in ``wisp.utils.privileges``.

The hint exists because ``sudo`` resets ``PATH`` and drops ``~/.local/bin``,
where ``uv tool install`` puts the ``wisp`` console script. A regression here
sends users to a command their shell reports as "command not found".
"""

from pathlib import Path

import pytest

from wisp.utils import privileges


def test_root_invocation_uses_absolute_console_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        privileges.shutil, "which", lambda name: "/home/u/.local/bin/wisp"
    )

    hint = privileges.root_invocation("daemon install")

    assert hint == (
        'sudo env PYTHONDONTWRITEBYTECODE=1 "/home/u/.local/bin/wisp" daemon install'
    )


def test_root_invocation_falls_back_to_argv_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "wisp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(privileges.shutil, "which", lambda name: None)
    monkeypatch.setattr(privileges.sys, "argv", [str(binary)])

    hint = privileges.root_invocation("daemon uninstall")

    assert hint == (
        f'sudo env PYTHONDONTWRITEBYTECODE=1 "{binary.resolve()}" daemon uninstall'
    )


def test_root_invocation_falls_back_to_bare_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(privileges.shutil, "which", lambda name: None)
    monkeypatch.setattr(privileges.sys, "argv", [str(tmp_path / "missing")])

    hint = privileges.root_invocation("daemon install")

    assert hint == 'sudo env PYTHONDONTWRITEBYTECODE=1 "wisp" daemon install'
