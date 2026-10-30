"""Tests for ActiveSession metadata, TTL calculations, and serialization."""

import json
from pathlib import Path

import pytest

import wisp.session as session_module
from wisp.session import ActiveSession, read_active_session, write_active_session


def test_active_session_defaults_when_empty() -> None:
    """An empty session dict must provide safe zero/empty defaults."""
    session = ActiveSession({})
    assert session.provider == ""
    assert session.region == ""
    assert session.pid == 0
    assert session.started_at == 0.0
    assert session.ttl_minutes == 0
    assert session.expires_at == 0.0
    assert session.has_ttl is False
    assert session.is_expired(now=1000.0) is False
    assert session.is_resumable(now=1000.0) is False
    assert session.vps == {}


def test_active_session_tolerates_corrupt_values() -> None:
    """Corrupt or mistyped metadata fields must fall back to zero defaults."""
    session = ActiveSession(
        {
            "pid": "invalid",
            "started_at": None,
            "ttl_minutes": "corrupt",
            "expires_at": "bad_float",
            "vps": "not_a_dict",
        }
    )
    assert session.pid == 0
    assert session.started_at == 0.0
    assert session.ttl_minutes == 0
    assert session.expires_at == 0.0
    assert session.has_ttl is False
    assert session.is_expired(now=500.0) is False
    assert session.is_resumable(now=500.0) is False
    assert session.vps == {}


def test_active_session_ttl_and_resumability() -> None:
    """Test TTL expiration and resumability boundaries."""
    session = ActiveSession(
        {
            "provider": "aws",
            "region": "us-east-1",
            "started_at": 1000.0,
            "ttl_minutes": 60,
            "expires_at": 4600.0,
        }
    )
    assert session.has_ttl is True

    # Before expiration: not expired, resumable
    assert session.is_expired(now=4599.0) is False
    assert session.is_resumable(now=4599.0) is True

    # At expiration: expired, not resumable
    assert session.is_expired(now=4600.0) is True
    assert session.is_resumable(now=4600.0) is False

    # After expiration: expired, not resumable
    assert session.is_expired(now=4601.0) is True
    assert session.is_resumable(now=4601.0) is False


def test_active_session_without_ttl_is_never_expired_or_resumable() -> None:
    """Sessions without TTL are neither expired nor resumable."""
    session = ActiveSession(
        {
            "provider": "aws",
            "region": "us-east-1",
            "started_at": 1000.0,
            "ttl_minutes": 0,
            "expires_at": 0.0,
        }
    )
    assert session.has_ttl is False
    assert session.is_expired(now=999999.0) is False
    assert session.is_resumable(now=999999.0) is False


def test_write_active_session_persists_ttl_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_active_session must persist ttl_minutes and computed expires_at."""
    marker_path = tmp_path / "active_session.json"
    monkeypatch.setattr(session_module, "WISP_ACTIVE_SESSION_PATH", marker_path)

    write_active_session("aws", "us-west-2", ttl_minutes=60)

    assert marker_path.is_file()
    payload = json.loads(marker_path.read_text(encoding="utf-8"))

    assert payload["provider"] == "aws"
    assert payload["region"] == "us-west-2"
    assert payload["ttl_minutes"] == 60
    assert payload["started_at"] > 0
    assert payload["expires_at"] == pytest.approx(
        payload["started_at"] + 3600.0, rel=1e-3
    )

    loaded = read_active_session()
    assert loaded is not None
    assert loaded.provider == "aws"
    assert loaded.region == "us-west-2"
    assert loaded.ttl_minutes == 60
    assert loaded.has_ttl is True
    assert loaded.expires_at == pytest.approx(payload["expires_at"], rel=1e-3)


def test_write_active_session_zero_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_active_session with ttl_minutes=0 must persist expires_at=0.0."""
    marker_path = tmp_path / "active_session.json"
    monkeypatch.setattr(session_module, "WISP_ACTIVE_SESSION_PATH", marker_path)

    write_active_session("oci", "us-ashburn-1", ttl_minutes=0)

    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["ttl_minutes"] == 0
    assert payload["expires_at"] == 0.0

    loaded = read_active_session()
    assert loaded is not None
    assert loaded.has_ttl is False
