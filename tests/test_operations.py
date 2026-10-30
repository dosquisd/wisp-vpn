"""Teardown notice: the only cross-process signal Wisp writes.

Mutual exclusion is Pulumi's job (its backend locks a stack during an update),
so there is deliberately no Wisp-owned lock to test here. What is tested is the
advisory notice a running TUI polls to notice that another process is destroying
its session.
"""

import json
import os

import pytest

from wisp.utils import operations


def _patch_notice_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the module at a temporary notice file."""
    path = tmp_path / "teardown-notice.json"
    monkeypatch.setattr(operations, "WISP_TEARDOWN_NOTICE_PATH", path)
    return path


def test_teardown_notice_round_trip(tmp_path, monkeypatch) -> None:
    """A notice written by this process is readable and then removed."""
    path = _patch_notice_path(tmp_path, monkeypatch)

    assert operations.read_teardown_notice() is None

    operations.begin_teardown("aws", "us-east-1")
    assert path.is_file()

    notice = operations.read_teardown_notice()
    assert notice is not None
    assert notice["provider"] == "aws"
    assert notice["region"] == "us-east-1"
    assert notice["pid"] == os.getpid()

    operations.end_teardown()
    assert operations.read_teardown_notice() is None
    assert not path.exists()


def test_notice_with_a_dead_pid_is_ignored(tmp_path, monkeypatch) -> None:
    """A notice left behind by a killed process must not freeze the TUI."""
    path = _patch_notice_path(tmp_path, monkeypatch)
    path.write_text(
        json.dumps(
            {
                "provider": "aws",
                "region": "us-east-1",
                "pid": 999_999,
                "started_at": 0.0,
            }
        ),
        encoding="utf-8",
    )

    assert operations.read_teardown_notice() is None


def test_stale_notice_expires(tmp_path, monkeypatch) -> None:
    """Age is evaluated from the injected clock, not the file's mtime."""
    _patch_notice_path(tmp_path, monkeypatch)
    operations.begin_teardown("aws", "us-east-1")

    notice = operations.read_teardown_notice()
    assert notice is not None
    started_at = notice["started_at"]

    fresh = started_at + operations.STALE_NOTICE_SECONDS - 1
    assert operations.read_teardown_notice(now=fresh) is not None

    stale = started_at + operations.STALE_NOTICE_SECONDS + 1
    assert operations.read_teardown_notice(now=stale) is None


def test_end_teardown_leaves_another_processes_notice(tmp_path, monkeypatch) -> None:
    """Only the writer clears its own notice."""
    path = _patch_notice_path(tmp_path, monkeypatch)
    path.write_text(
        json.dumps(
            {
                "provider": "aws",
                "region": "us-east-1",
                "pid": os.getppid(),
                "started_at": 0.0,
            }
        ),
        encoding="utf-8",
    )

    operations.end_teardown()

    assert path.is_file()


def test_malformed_notice_never_raises(tmp_path, monkeypatch) -> None:
    """A half-written file must degrade to "no notice"."""
    path = _patch_notice_path(tmp_path, monkeypatch)

    path.write_text("", encoding="utf-8")
    assert operations.read_teardown_notice() is None

    path.write_text("{not json", encoding="utf-8")
    assert operations.read_teardown_notice() is None

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert operations.read_teardown_notice() is None
