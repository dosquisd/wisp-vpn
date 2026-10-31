"""Tests for VPS package provenance and the destroy-time cleanup."""

import pytest

from wisp.wireguard import remote_server
from wisp.wireguard.remote_server import (
    _packages_to_install,
    _write_installed_packages,
    remove_installed_packages,
)

MANIFEST_READ = "cat /etc/wisp/installed-packages"
FOREIGN_LISTING = "ls /etc/wireguard/*.conf"
FAMILY_PROBE = "for m in apt-get"
TOOL_PROBE = "for t in wg qrencode"


class _FakeSSH:
    """Stand-in for a connected Paramiko client."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[str, tuple[int, str, str]]],
) -> tuple[_FakeSSH, list[str]]:
    """Script SSH and capture every remote command.

    Args:
        monkeypatch: Pytest fixture.
        responses: ``(needle, (status, stdout, stderr))`` pairs, checked in
            order against the command; the first match wins.

    Returns:
        tuple[_FakeSSH, list[str]]: The fake client and the captured commands.
    """
    calls: list[str] = []

    def fake_run(_ssh, command):
        calls.append(command)
        for needle, result in responses:
            if needle in command:
                return result
        return (0, "", "")

    ssh = _FakeSSH()
    monkeypatch.setattr(remote_server, "_connect_ssh", lambda *a, **kw: ssh)
    monkeypatch.setattr(remote_server, "_run_remote", fake_run)
    return ssh, calls


def test_provenance_records_only_the_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that already has `wg` must not record WireGuard as Wisp's."""
    ssh, _ = _stub(
        monkeypatch,
        [(FAMILY_PROBE, (0, "apt-get\n", "")), (TOOL_PROBE, (0, "qrencode\n", ""))],
    )

    assert _packages_to_install(ssh, "ubuntu") == ("qrencode",)


def test_provenance_includes_wireguard_on_apt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debian/Ubuntu ship the metapackage plus its tools."""
    ssh, _ = _stub(
        monkeypatch,
        [(FAMILY_PROBE, (0, "apt-get\n", "")), (TOOL_PROBE, (0, "wg\n", ""))],
    )

    assert _packages_to_install(ssh, "ubuntu") == ("wireguard", "wireguard-tools")


def test_provenance_uses_alpine_package_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alpine calls the QR tool `libqrencode-tools`."""
    ssh, _ = _stub(
        monkeypatch,
        [
            (FAMILY_PROBE, (0, "apk\n", "")),
            (TOOL_PROBE, (0, "wg\nqrencode\n", "")),
        ],
    )

    assert _packages_to_install(ssh, "alpine") == (
        "wireguard-tools",
        "libqrencode-tools",
    )


def test_provenance_is_empty_when_everything_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing missing means nothing for Wisp to claim."""
    ssh, _ = _stub(
        monkeypatch,
        [(FAMILY_PROBE, (0, "apt-get\n", "")), (TOOL_PROBE, (0, "", ""))],
    )

    assert _packages_to_install(ssh, "ubuntu") == ()


def test_provenance_is_empty_when_the_family_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized package manager disables the whole path."""
    ssh, calls = _stub(monkeypatch, [(FAMILY_PROBE, (0, "", ""))])

    assert _packages_to_install(ssh, "ubuntu") == ()
    assert not any(TOOL_PROBE in command for command in calls)


def test_manifest_write_is_private_and_lists_each_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest is root-only and one package per line."""
    ssh, calls = _stub(monkeypatch, [])

    assert _write_installed_packages(ssh, "ubuntu", ("wireguard", "qrencode"))

    command = calls[0]
    assert 'printf "%s\\n" wireguard qrencode' in command
    assert "chmod 600 /etc/wisp/installed-packages" in command


def test_manifest_write_skips_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """No packages to claim means no manifest, not an empty one."""
    ssh, calls = _stub(monkeypatch, [])

    assert _write_installed_packages(ssh, "ubuntu", ())

    assert calls == []


def test_cleanup_skips_when_the_server_has_its_own_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign config means the tooling now serves something else."""
    ssh, calls = _stub(
        monkeypatch,
        [(FOREIGN_LISTING, (0, "/etc/wireguard/office.conf\n", ""))],
    )

    assert remove_installed_packages("ubuntu", "203.0.113.9") is True

    assert not any("remove -y" in command for command in calls)
    assert ssh.closed is True


def test_cleanup_removes_only_the_allowed_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iptables/resolvconf are recorded but must never be removed."""
    _, calls = _stub(
        monkeypatch,
        [
            (FOREIGN_LISTING, (0, "", "")),
            (
                MANIFEST_READ,
                (0, "wireguard\nwireguard-tools\niptables\nresolvconf\nqrencode\n", ""),
            ),
            (FAMILY_PROBE, (0, "apt-get\n", "")),
        ],
    )

    assert remove_installed_packages("ubuntu", "203.0.113.9") is True

    removal = next(command for command in calls if "remove -y" in command)
    assert removal == "sudo -n apt-get remove -y wireguard wireguard-tools qrencode"
    assert "iptables" not in removal
    assert "resolvconf" not in removal
    assert any("rm -f /etc/wisp/installed-packages" in command for command in calls)


def test_cleanup_uses_the_family_detected_on_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dnf servers get a dnf command, not an apt one."""
    _, calls = _stub(
        monkeypatch,
        [
            (FOREIGN_LISTING, (0, "", "")),
            (MANIFEST_READ, (0, "wireguard-tools\nqrencode\n", "")),
            (FAMILY_PROBE, (0, "dnf\n", "")),
        ],
    )

    assert remove_installed_packages("fedora", "203.0.113.9") is True

    removal = next(command for command in calls if "remove -y" in command)
    assert removal == "sudo -n dnf remove -y wireguard-tools qrencode"


def test_cleanup_is_a_noop_without_a_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deploy that never recorded anything removes nothing."""
    _, calls = _stub(
        monkeypatch,
        [
            (FOREIGN_LISTING, (0, "", "")),
            (MANIFEST_READ, (1, "", "cat: no such file")),
        ],
    )

    assert remove_installed_packages("ubuntu", "203.0.113.9") is False

    assert not any("remove -y" in command for command in calls)


def test_cleanup_keeps_the_manifest_when_removal_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed removal must stay retryable."""
    _, calls = _stub(
        monkeypatch,
        [
            (FOREIGN_LISTING, (0, "", "")),
            (MANIFEST_READ, (0, "wireguard-tools\n", "")),
            (FAMILY_PROBE, (0, "apt-get\n", "")),
            ("apt-get remove", (100, "", "dpkg is locked")),
        ],
    )

    assert remove_installed_packages("ubuntu", "203.0.113.9") is False

    assert not any("rm -f /etc/wisp/installed-packages" in command for command in calls)


def test_cleanup_never_raises_when_the_connection_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not turn a finished teardown into a crash."""
    ssh = _FakeSSH()

    def explode(*_args, **_kwargs):
        raise OSError("Connection reset by peer")

    monkeypatch.setattr(remote_server, "_connect_ssh", lambda *a, **kw: ssh)
    monkeypatch.setattr(remote_server, "_run_remote", explode)

    assert remove_installed_packages("ubuntu", "203.0.113.9") is False
    assert ssh.closed is True


def test_cleanup_runs_without_sudo_when_connected_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root connections must not shell out to sudo."""
    _, calls = _stub(
        monkeypatch,
        [
            (FOREIGN_LISTING, (0, "", "")),
            (MANIFEST_READ, (0, "qrencode\n", "")),
            (FAMILY_PROBE, (0, "pacman\n", "")),
        ],
    )

    assert remove_installed_packages("root", "203.0.113.9") is True

    removal = next(command for command in calls if "pacman -Rs" in command)
    assert removal == "pacman -Rs --noconfirm qrencode"
