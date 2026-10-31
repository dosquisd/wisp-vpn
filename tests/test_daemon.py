"""Tests for the daemon's client-config validation (socket hardening)."""

import subprocess
import time
from pathlib import Path

import pytest

from wisp.daemon.adapter import (
    MANAGED_HEADER,
    BaseAdapter,
    LinuxAdapter,
    WindowsAdapter,
    _parse_latest_handshakes,
    with_ownership_header,
)
from wisp.daemon.server import (
    handle_connect,
    handle_disconnect,
    handle_status,
    reset_stale_tunnel,
    validate_client_config,
)
from wisp.daemon.server import (
    main as server_main,
)

CLEAN_CLIENT_CONFIG = """[Interface]
PrivateKey = abc123=
Address = 10.66.66.2/32
DNS = 1.1.1.1

[Peer]
PublicKey = def456=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 203.0.113.10:51820
"""


@pytest.mark.parametrize(
    "hook",
    ["PreUp", "PostUp", "PreDown", "PostDown"],
)
def test_rejects_script_hook_directives(hook: str) -> None:
    """wg-quick runs these directives as root: they must never reach disk."""
    content = CLEAN_CLIENT_CONFIG.replace(
        "[Peer]", f"{hook} = touch /tmp/pwned\n\n[Peer]"
    )
    with pytest.raises(ValueError, match="Directive not allowed"):
        validate_client_config(content)


def test_rejects_directive_case_insensitively() -> None:
    content = CLEAN_CLIENT_CONFIG.replace("[Peer]", "postup = evil\n\n[Peer]")
    with pytest.raises(ValueError, match="postup"):
        validate_client_config(content)


def test_rejects_directive_with_leading_whitespace() -> None:
    content = CLEAN_CLIENT_CONFIG.replace("[Peer]", "  PostUp = evil\n\n[Peer]")
    with pytest.raises(ValueError, match="postup"):
        validate_client_config(content)


def test_ignores_directive_names_inside_comments() -> None:
    """A commented mention is prose, not an executable hook."""
    content = CLEAN_CLIENT_CONFIG.replace(
        "[Peer]", "# PostUp = would-be-evil\n\n[Peer]"
    )
    validate_client_config(content)  # does not raise


def test_accepts_clean_client_config() -> None:
    validate_client_config(CLEAN_CLIENT_CONFIG)  # does not raise


# --- Stale tunnel detection and reset -----------------------------------------


class FakeAdapter:
    """Fake adapter for testing reset_stale_tunnel without real system calls."""

    def __init__(
        self,
        handshake: int | None = None,
        status_code: int = 0,
        disconnect_exc: Exception | None = None,
        managed: bool = True,
        foreign_reason: str | None = None,
    ) -> None:
        self._handshake = handshake
        self._status_code = status_code
        self._disconnect_exc = disconnect_exc
        self._managed = managed
        self._foreign_reason = foreign_reason
        self.disconnect_calls = 0
        self.connect_calls: list[str] = []

    def connect(self, config_content: str) -> subprocess.CompletedProcess:
        self.connect_calls.append(config_content)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def is_managed(self) -> bool:
        return self._managed

    def foreign_tunnel_reason(self) -> str | None:
        return self._foreign_reason

    def latest_handshake_epoch(self) -> int | None:
        return self._handshake

    def status(self) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], self._status_code, stdout="", stderr="")

    def disconnect(self) -> subprocess.CompletedProcess:
        self.disconnect_calls += 1
        if self._disconnect_exc:
            raise self._disconnect_exc
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")


async def test_reset_stale_tunnel_no_handshake_info() -> None:
    adapter = FakeAdapter(handshake=None)
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is False
    assert adapter.disconnect_calls == 0


async def test_reset_stale_tunnel_fresh_handshake() -> None:
    adapter = FakeAdapter(handshake=990)  # now - 10 <= STALE_TUNNEL_SECONDS
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is False
    assert adapter.disconnect_calls == 0


async def test_reset_stale_tunnel_stale_handshake() -> None:
    adapter = FakeAdapter(handshake=400)  # now - 600 > STALE_TUNNEL_SECONDS
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is True
    assert adapter.disconnect_calls == 1


async def test_reset_stale_tunnel_handshake_zero_status_down() -> None:
    adapter = FakeAdapter(handshake=0, status_code=1)
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is False
    assert adapter.disconnect_calls == 0


async def test_reset_stale_tunnel_handshake_zero_status_up() -> None:
    adapter = FakeAdapter(handshake=0, status_code=0)
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is True
    assert adapter.disconnect_calls == 1


async def test_reset_stale_tunnel_disconnect_called_process_error() -> None:
    exc = subprocess.CalledProcessError(1, ["wg-quick", "down", "wg0"])
    adapter = FakeAdapter(handshake=400, disconnect_exc=exc)
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is False
    assert adapter.disconnect_calls == 1


async def test_reset_stale_tunnel_disconnect_generic_exception() -> None:
    exc = RuntimeError("network glitch")
    adapter = FakeAdapter(handshake=400, disconnect_exc=exc)
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is False
    assert adapter.disconnect_calls == 1


async def test_reset_stale_tunnel_skips_foreign_tunnel() -> None:
    """A stale wg0 that Wisp did not create must survive the daemon startup."""
    adapter = FakeAdapter(handshake=400, managed=False)  # stale, but not ours
    result = await reset_stale_tunnel(adapter, now=1000.0)
    assert result is False
    assert adapter.disconnect_calls == 0


async def test_server_main_calls_reset_stale_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    class DummyTransport:
        def __init__(self) -> None:
            self.adapter = FakeAdapter(handshake=None)

        async def serve(self, handler) -> None:
            pass

    async def fake_reset(adapter, now=None):
        nonlocal called
        called = True
        return False

    monkeypatch.setattr("wisp.daemon.server.reset_stale_tunnel", fake_reset)
    await server_main(DummyTransport())
    assert called is True


async def test_server_main_survives_reset_stale_tunnel_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = False

    class DummyTransport:
        def __init__(self) -> None:
            self.adapter = FakeAdapter(handshake=None)

        async def serve(self, handler) -> None:
            nonlocal served
            served = True

    async def exploding_reset(adapter, now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("wisp.daemon.server.reset_stale_tunnel", exploding_reset)
    await server_main(DummyTransport())
    assert served is True


# --- Tunnel ownership (never touch someone else's WireGuard config) ----------


def _linux_adapter_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    content: str | None = None,
) -> LinuxAdapter:
    """Point a LinuxAdapter at a temp config, optionally pre-created."""
    conf = tmp_path / "wg0.conf"
    if content is not None:
        conf.write_text(content, encoding="utf-8")
    monkeypatch.setattr(LinuxAdapter, "WG_CONF_PATH", conf)
    return LinuxAdapter()


def test_is_managed_requires_the_ownership_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    managed = _linux_adapter_at(
        monkeypatch, tmp_path, content=f"{MANAGED_HEADER}\n{CLEAN_CLIENT_CONFIG}"
    )
    assert managed.is_managed() is True


def test_is_managed_rejects_foreign_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    foreign = _linux_adapter_at(monkeypatch, tmp_path, content=CLEAN_CLIENT_CONFIG)
    assert foreign.is_managed() is False


def test_is_managed_is_false_without_a_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _linux_adapter_at(monkeypatch, tmp_path).is_managed() is False


def test_foreign_tunnel_reason_is_none_when_the_name_is_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = _linux_adapter_at(monkeypatch, tmp_path)
    monkeypatch.setattr(
        adapter, "status", lambda: subprocess.CompletedProcess([], 1, stdout="")
    )
    assert adapter.foreign_tunnel_reason() is None


def test_foreign_tunnel_reason_names_an_unmanaged_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = _linux_adapter_at(monkeypatch, tmp_path, content=CLEAN_CLIENT_CONFIG)
    reason = adapter.foreign_tunnel_reason()
    assert reason is not None
    assert "wg0" in reason
    assert str(tmp_path / "wg0.conf") in reason


def test_foreign_tunnel_reason_names_a_live_unmanaged_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No config of ours, but the interface is up: someone else owns the name."""
    adapter = _linux_adapter_at(monkeypatch, tmp_path)
    monkeypatch.setattr(
        adapter, "status", lambda: subprocess.CompletedProcess([], 0, stdout="peer")
    )
    assert adapter.foreign_tunnel_reason() is not None


def test_foreign_tunnel_reason_is_none_for_a_managed_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = _linux_adapter_at(
        monkeypatch, tmp_path, content=f"{MANAGED_HEADER}\n{CLEAN_CLIENT_CONFIG}"
    )
    assert adapter.foreign_tunnel_reason() is None


def test_with_ownership_header_precedes_the_config() -> None:
    assert with_ownership_header(CLEAN_CLIENT_CONFIG) == (
        f"{MANAGED_HEADER}\n{CLEAN_CLIENT_CONFIG}"
    )


async def test_handle_connect_refuses_a_foreign_tunnel() -> None:
    reason = "wg0 already exists and was not created by Wisp"
    adapter = FakeAdapter(managed=False, foreign_reason=reason)

    response = await handle_connect(CLEAN_CLIENT_CONFIG, adapter)

    assert response.ok is False
    assert response.message == reason
    assert adapter.connect_calls == []


async def test_handle_connect_marks_the_config_as_owned() -> None:
    adapter = FakeAdapter(managed=True)

    response = await handle_connect(CLEAN_CLIENT_CONFIG, adapter)

    assert response.ok is True
    assert adapter.connect_calls == [f"{MANAGED_HEADER}\n{CLEAN_CLIENT_CONFIG}"]


async def test_handle_disconnect_refuses_a_foreign_tunnel() -> None:
    adapter = FakeAdapter(managed=False, foreign_reason="not ours")

    response = await handle_disconnect(adapter)

    assert response.ok is False
    assert adapter.disconnect_calls == 0


async def test_handle_status_hides_handshake_age_for_a_foreign_tunnel() -> None:
    """Reporting another VPN's handshakes would make the TUI adopt the wrong tunnel."""
    adapter = FakeAdapter(handshake=990, managed=False)

    response = await handle_status(adapter)

    assert response.handshake_age_seconds is None


async def test_handle_status_reports_handshake_age_for_a_managed_tunnel() -> None:
    adapter = FakeAdapter(handshake=int(time.time()) - 5, managed=True)

    response = await handle_status(adapter)

    assert response.handshake_age_seconds is not None
    assert 0 <= response.handshake_age_seconds <= 5


async def test_handle_status_exposes_a_tunnel_conflict() -> None:
    """Deploy callers refuse before provisioning when the name belongs to another VPN."""
    adapter = FakeAdapter(managed=False, foreign_reason="wg0 is not ours")

    response = await handle_status(adapter)

    assert response.tunnel_conflict == "wg0 is not ours"


async def test_handle_status_reports_no_conflict_for_a_managed_tunnel() -> None:
    response = await handle_status(FakeAdapter(managed=True))

    assert response.tunnel_conflict is None


# --- Adapter handshake inspection & Windows start configuration --------------


def test_base_adapter_default_latest_handshake_epoch() -> None:
    class StubAdapter(BaseAdapter):
        def connect(self, config_content: str):
            pass

        def disconnect(self):
            pass

        def status(self):
            pass

    adapter = StubAdapter()
    assert adapter.latest_handshake_epoch() is None


def test_parse_latest_handshakes_empty_or_malformed() -> None:
    assert _parse_latest_handshakes("") is None
    assert _parse_latest_handshakes("   \n\t\n") is None
    assert _parse_latest_handshakes("single_token_only") is None
    assert _parse_latest_handshakes("peer invalid_number") is None


def test_parse_latest_handshakes_single_and_multiple() -> None:
    assert _parse_latest_handshakes("pubkey1 0") == 0
    assert _parse_latest_handshakes("pubkey1 1600000000") == 1600000000
    multi = "pubkey1 1600000100\npubkey2 1600000300\npubkey3 1600000200"
    assert _parse_latest_handshakes(multi) == 1600000300


def test_linux_adapter_latest_handshake_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = LinuxAdapter()

    # Success case
    def fake_run_ok(cmd, **kwargs):
        assert cmd == ["wg", "show", "wg0", "latest-handshakes"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="key1 1700000500\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    assert adapter.latest_handshake_epoch() == 1700000500

    # Failure case
    def fake_run_fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="interface down")

    monkeypatch.setattr(subprocess, "run", fake_run_fail)
    assert adapter.latest_handshake_epoch() is None

    # OSError case
    def fake_run_oserror(cmd, **kwargs):
        raise FileNotFoundError("wg not found")

    monkeypatch.setattr(subprocess, "run", fake_run_oserror)
    assert adapter.latest_handshake_epoch() is None


def test_windows_adapter_latest_handshake_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = WindowsAdapter()

    def fake_run_ok(cmd, **kwargs):
        assert cmd == ["wg", "show", "wg0", "latest-handshakes"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="keyA 1700000600\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    assert adapter.latest_handshake_epoch() == 1700000600

    def fake_run_fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error")

    monkeypatch.setattr(subprocess, "run", fake_run_fail)
    assert adapter.latest_handshake_epoch() is None


def test_windows_adapter_connect_configures_manual_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = WindowsAdapter()
    adapter.WG_CONF_PATH = tmp_path / "wg0.conf"

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(adapter, "_run", fake_run)
    monkeypatch.setattr("wisp.daemon.adapter.secure_file", lambda *a, **kw: None)

    adapter.connect(CLEAN_CLIENT_CONFIG)

    # Must have called sc.exe config WireGuardTunnel$wg0 start= demand
    assert ["sc.exe", "config", "WireGuardTunnel$wg0", "start=", "demand"] in calls


def test_windows_adapter_connect_survives_sc_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = WindowsAdapter()
    adapter.WG_CONF_PATH = tmp_path / "wg0.conf"

    def fake_sub_run(cmd, *args, **kwargs):
        if cmd[0] == "sc.exe":
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Access denied"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_adapter_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_sub_run)
    monkeypatch.setattr(adapter, "_run", fake_adapter_run)
    monkeypatch.setattr("wisp.daemon.adapter.secure_file", lambda *a, **kw: None)

    # Should not raise even though sc.exe failed
    proc = adapter.connect(CLEAN_CLIENT_CONFIG)
    assert proc.returncode == 0


async def test_handle_status_handshake_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify handle_status populates handshake_age_seconds from latest_handshake_epoch."""
    from wisp.daemon.server import handle_status

    monkeypatch.setattr("wisp.daemon.server.time.time", lambda: 1050.0)

    # Positive epoch -> age calculated
    adapter = FakeAdapter(handshake=1000)
    resp = await handle_status(adapter)
    assert resp.handshake_age_seconds == 50
    assert resp.ok is True

    # None -> None
    adapter_none = FakeAdapter(handshake=None)
    resp_none = await handle_status(adapter_none)
    assert resp_none.handshake_age_seconds is None

    # 0 -> None
    adapter_zero = FakeAdapter(handshake=0)
    resp_zero = await handle_status(adapter_zero)
    assert resp_zero.handshake_age_seconds is None
