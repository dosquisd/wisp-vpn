"""Guard the vendored WireGuard installer's IPv4-only contract.

Wisp disables IPv6 *inside* the tunnel on purpose: `wg-quick up` fails on a
kernel booted with IPv6 disabled when it tries to add the interface's IPv6
address, and it fails on any kernel when AllowedIPs makes it install a `::/0`
route the kernel cannot serve. The installer is vendored from upstream, so a
future sync of that file must not silently reintroduce IPv6 plumbing.

These tests read the packaged script as data; nothing is executed. Comment
lines are excluded because the file's header documents what is deliberately
absent.
"""

from wisp.config.constants import WIREGUARD_SCRIPT_PATH


def _script() -> str:
    """Return the packaged installer script contents.

    Returns:
        str: The full script text, comments included.
    """
    return WIREGUARD_SCRIPT_PATH.read_text(encoding="utf-8")


def _code() -> str:
    """Return the installer script with full-line comments removed.

    Returns:
        str: The executable part of the script.
    """
    return "\n".join(
        line for line in _script().splitlines() if not line.lstrip().startswith("#")
    )


def test_no_ipv6_tunnel_plumbing() -> None:
    """No IPv6 interface address, firewall rule or forwarding setting remains."""
    code = _code()

    assert "SERVER_WG_IPV6=" not in code
    assert "CLIENT_WG_IPV6=" not in code
    assert "ip6tables" not in code
    assert "net.ipv6.conf.all.forwarding" not in code
    assert "Address = ${SERVER_WG_IPV4}/24,${SERVER_WG_IPV6}/64" not in code
    assert "Address = ${CLIENT_WG_IPV4}/32,${CLIENT_WG_IPV6}/128" not in code


def test_tunnel_addresses_are_ipv4_only() -> None:
    """Both generated interfaces get exactly one IPv4 address."""
    code = _code()

    assert "Address = ${SERVER_WG_IPV4}/24" in code
    assert "Address = ${CLIENT_WG_IPV4}/32" in code
    assert 'AllowedIPs = ${CLIENT_WG_IPV4}/32"' in code


def test_allowed_ips_defaults_to_ipv4_and_strips_ipv6() -> None:
    """The default routes IPv4 only, and a caller-supplied list is sanitized."""
    code = _code()

    assert 'ALLOWED_IPS="0.0.0.0/0,::/0"' not in code
    assert 'ALLOWED_IPS="0.0.0.0/0"' in code
    assert "-i '0.0.0.0/0'" in code
    # The sanitizer drops any entry containing a colon (an IPv6 address).
    assert "if [[ ${ENTRY} == *:* ]]; then" in code
