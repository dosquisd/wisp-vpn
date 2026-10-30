"""Tests for the daemon's client-config validation (socket hardening)."""

import subprocess
from pathlib import Path

import pytest

from wisp.daemon.adapter import (
    BaseAdapter,
    LinuxAdapter,
    WindowsAdapter,
    _parse_latest_handshakes,
)
from wisp.daemon.server import (
    main as server_main,
)
from wisp.daemon.server import (
    reset_stale_tunnel,
    validate_client_config,
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
    ) -> None:
        self._handshake = handshake
        self._status_code = status_code
        self._disconnect_exc = disconnect_exc
        self.disconnect_calls = 0

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
