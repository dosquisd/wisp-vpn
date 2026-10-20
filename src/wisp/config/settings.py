"""Global and per-session configuration.

The global configuration is loaded from a single user-level ``wisp.toml`` —
this is the first thing Wisp loads at startup. It holds provider credentials,
per-provider defaults, and general defaults (:data:`get_default_provider`,
:data:`get_default_region_for`). Its location is ``WISP_CONFIG_DIR/wisp.toml``
(``$WISP_CONFIG_DIR`` when set, else ``platformdirs.user_config_path("wisp")``);
the project-level ``wisp.toml`` at the repo root is no longer read.

:class:`WispConfig` is the per-session deployment settings object, built from
the ``[general]`` TOML section via :func:`load_wisp_config` and edited in
memory by the TUI Configuration screen (never persisted back to disk).

Provider-specific values (``[aws]``, ``[oci]``) are resolved by
:mod:`wisp.config.credentials`, including each provider's own default region.
There is deliberately no cross-provider default region.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wisp.config.constants import (
    DEFAULT_SESSION_TTL_MINUTES,
    DEFAULT_VM_BOOT_TIMEOUT_SECONDS,
    WIREGUARD_DNS1,
    WIREGUARD_DNS2,
    WIREGUARD_INTERFACE,
    WIREGUARD_IPV4,
    WIREGUARD_IPV6,
    WISP_USER_CONFIG_PATH,
)

# ─── Global configuration (wisp.toml) ────────────────────────────

_cached_config: dict[str, Any] | None = None


def _read_toml_file(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file, returning an empty dict on failure.

    Args:
        path (Path): Path to the TOML file.

    Returns:
        dict[str, Any]: Parsed TOML contents, or empty dict if the file
            does not exist, is unreadable, or is invalid.
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):  # fmt: skip
        return {}


def load_toml_config() -> dict[str, Any]:
    """Load the global TOML configuration (cached after first load).

    Reads from a single user-level location: ``wisp.toml`` in the Wisp
    config directory (``$WISP_CONFIG_DIR`` if set, otherwise
    ``platformdirs.user_config_path("wisp")``). See
    :func:`get_active_config_path` for details.

    The parsed result is cached; subsequent calls return the same dict
    without re-reading the file. Use :func:`reload_toml_config` to
    force a fresh read (e.g. after editing values).

    Returns:
        dict[str, Any]: Config as a dict, or empty dict if no
            config file was found.
    """
    global _cached_config
    if _cached_config is None:
        _cached_config = _merge_toml_files()
    return _cached_config


def reload_toml_config() -> dict[str, Any]:
    """Force a fresh read of the global TOML configuration.

    Clears the cache and re-reads the config file, returning the new
    result.

    Returns:
        dict[str, Any]: Freshly read config as a dict.
    """
    global _cached_config
    _cached_config = _merge_toml_files()
    return _cached_config


def _merge_toml_files() -> dict[str, Any]:
    """Read the single user-level TOML file (no caching).

    Returns:
        dict[str, Any]: Parsed config, or an empty dict when the file is
            missing or invalid.
    """
    parsed = _read_toml_file(WISP_USER_CONFIG_PATH)
    if not parsed:
        return {}
    # Return as-is (shallow structure is fine for top-level sections)
    return parsed


def get_active_config_path() -> Path:
    """Return the single wisp.toml used as both read source and write target.

    Wisp has exactly one config file now — there is no user/project merge and
    therefore nothing to disambiguate. The TUI's Save action writes here.

    Returns:
        Path: ``WISP_USER_CONFIG_PATH`` (``WISP_CONFIG_DIR/wisp.toml``).
    """
    return WISP_USER_CONFIG_PATH


_DEFAULT_CONFIG_HEADER = (
    "# Wisp configuration file. All values are optional; defaults apply when\n"
    "# omitted. Full example with every section documented:\n"
    "# https://github.com/dosquisd/wisp-vpn/blob/main/wisp.toml.example\n"
)


def ensure_user_config() -> Path:
    """Ensure the user-level ``wisp.toml`` exists, creating it when missing.

    The single config location is :data:`WISP_USER_CONFIG_PATH`, and nothing
    else creates it — so a fresh install or a wiped config directory would
    otherwise leave the CLI running on pure defaults with no hint of where
    settings belong. Creation is explicit (called from the CLI/TUI entry
    points, never at import time) and never touches an existing file: user
    edits are sacred.

    Returns:
        Path: The user config path, existing or freshly created.
    """
    from wisp.utils.platform import secure_file  # deferred: avoid import cycle

    if WISP_USER_CONFIG_PATH.exists():
        return WISP_USER_CONFIG_PATH

    WISP_USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    WISP_USER_CONFIG_PATH.write_text(_DEFAULT_CONFIG_HEADER, encoding="utf-8")
    secure_file(WISP_USER_CONFIG_PATH)
    return WISP_USER_CONFIG_PATH


def describe_config_sources() -> str:
    """Rich-markup one-liner naming the active wisp.toml, for TUI/log display.

    Returns:
        str: A Rich markup string showing the single active config path.
    """
    return f"[dim]Active config:[/dim] [cyan]{WISP_USER_CONFIG_PATH}[/cyan]"


def get_default_provider() -> str:
    """Return the default provider from ``[general]`` (fallback: ``"aws"``)."""
    return load_toml_config().get("general", {}).get("default_provider", "aws")


def get_default_region_for(provider: str) -> str:
    """Return the default region configured for a provider section.

    Regions are provider-specific (e.g. ``[aws].region``, ``[oci].region``);
    there is no cross-provider default region.

    Args:
        provider (str): Provider section name (``"aws"`` or ``"oci"``).

    Returns:
        str: The configured region, or ``""`` if not set.
    """
    return str(load_toml_config().get(provider, {}).get("region", ""))


# ─── Writing back to disk ─────────────────────────────────────────


def _write_toml_raw(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` as TOML to a single file, creating/securing it.

    Requires the ``tomli-w`` package.
    """
    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(data, f)

    from wisp.utils.platform import secure_file  # deferred: avoid import cycle

    secure_file(path)


def save_session_config(
    cfg: "WispConfig",
    provider_regions: dict[str, str] | None = None,
    path: Path | None = None,
) -> Path:
    """Persist ``cfg`` (and optionally per-provider regions) to one wisp.toml.

    Only two things in the target file are touched:
    - The ``[general]`` section is *updated* (not replaced) with ``cfg``'s
      fields, so unrelated keys already there — e.g. ``default_provider``,
      which lives in ``[general]`` but isn't part of :class:`WispConfig` —
      survive the save.
    - For each ``provider: region`` pair in ``provider_regions``, only that
      section's ``region`` key is set (or removed, if ``region`` is empty).
      Everything else in that section is left exactly as-is.

    Defaults to :func:`get_active_config_path` when ``path`` isn't given.

    Args:
        cfg (WispConfig): Per-session settings to persist.
        provider_regions (dict[str, str] | None): Optional ``provider: region``
            pairs whose section ``region`` key is updated. ``None`` leaves
            provider sections untouched.
        path (Path | None): Explicit write target; defaults to the active
            config path.

    Returns:
        Path: The file that was actually written.
    """
    target = path or get_active_config_path()
    data = _read_toml_file(target)

    general_section = data.setdefault("general", {})
    general_section.update(
        {
            "vm_boot_timeout": cfg.vm_boot_timeout,
            "wireguard_interface": cfg.wireguard_interface,
            "wireguard_ipv4": cfg.wireguard_ipv4,
            "wireguard_ipv6": cfg.wireguard_ipv6,
            "wireguard_dns1": cfg.wireguard_dns1,
            "wireguard_dns2": cfg.wireguard_dns2,
            "wireguard_port": cfg.wireguard_port,
            "force_current_ip": cfg.force_current_ip,
            "confirm_destroy": cfg.confirm_destroy,
            "max_session_ttl_minutes": cfg.max_session_ttl_minutes,
        }
    )

    for provider, region in (provider_regions or {}).items():
        section = data.setdefault(provider, {})
        if region:
            section["region"] = region
        else:
            section.pop("region", None)

    _write_toml_raw(target, data)
    reload_toml_config()
    return target


# ─── Per-session configuration ───────────────────────────────────


@dataclass
class WispConfig:
    """Per-session deployment settings.

    Edited in memory by the TUI Configuration screen (not persisted to disk).
    Defaults are drawn from :mod:`wisp.config.constants`.
    """

    vm_boot_timeout: int = DEFAULT_VM_BOOT_TIMEOUT_SECONDS
    wireguard_interface: str = WIREGUARD_INTERFACE
    wireguard_ipv4: str = WIREGUARD_IPV4
    wireguard_ipv6: str = WIREGUARD_IPV6
    wireguard_dns1: str = WIREGUARD_DNS1
    wireguard_dns2: str = WIREGUARD_DNS2
    wireguard_port: int = 0  # 0 indicates dynamic random port (49152-65535)
    force_current_ip: bool = False
    confirm_destroy: bool = True
    max_session_ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES


def load_wisp_config() -> WispConfig:
    """Build a :class:`WispConfig` from the ``[general]`` TOML section.

    Any field omitted in the TOML falls back to the constant defaults from
    :mod:`wisp.config.constants`.

    Returns:
        WispConfig: Per-session settings populated from ``wisp.toml``.
    """
    general = load_toml_config().get("general", {})
    return WispConfig(
        vm_boot_timeout=general.get("vm_boot_timeout", DEFAULT_VM_BOOT_TIMEOUT_SECONDS),
        wireguard_interface=general.get("wireguard_interface", WIREGUARD_INTERFACE),
        wireguard_ipv4=general.get("wireguard_ipv4", WIREGUARD_IPV4),
        wireguard_ipv6=general.get("wireguard_ipv6", WIREGUARD_IPV6),
        wireguard_dns1=general.get("wireguard_dns1", WIREGUARD_DNS1),
        wireguard_dns2=general.get("wireguard_dns2", WIREGUARD_DNS2),
        wireguard_port=int(general.get("wireguard_port", 0)),
        force_current_ip=bool(general.get("force_current_ip", False)),
        confirm_destroy=bool(general.get("confirm_destroy", True)),
        max_session_ttl_minutes=int(
            general.get("max_session_ttl_minutes", DEFAULT_SESSION_TTL_MINUTES)
        ),
    )
