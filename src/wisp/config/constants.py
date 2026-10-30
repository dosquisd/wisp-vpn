"""Project-wide constants: tags, Pulumi identity, filesystem paths, and
WireGuard defaults.

Paths resolve using `platformdirs` (data/config/log directories) with
optional environment overrides (`WISP_DATA_DIR`, `WISP_CONFIG_DIR`,
`WISP_LOG_DIR`). Declared constants are `Path` objects computed without
filesystem access; directory creation is performed explicitly by helper
functions called by code paths that actually need them.
"""

import os
from importlib.resources import files
from pathlib import Path

import platformdirs

CREATED_BY_TAG: str = "wisp"
PULUMI_PROJECT_NAME: str = "wisp-project"


def get_pulumi_stack_name(provider: str) -> str:
    """Get provider-specific Pulumi stack name."""
    return f"wisp-stack-{provider}"


DEFAULT_TAG = {
    "created-by": CREATED_BY_TAG,
}

DEFAULT_VM_BOOT_TIMEOUT_SECONDS: int = 60

# Default session TTL for the optional OS-level watchdog (1 hour).
DEFAULT_SESSION_TTL_MINUTES: int = 60

# Default idle timeout before the in-VM watchdog triggers teardown (15 minutes).
DEFAULT_WATCHDOG_IDLE_SECONDS: int = 60 * 15


def _data_dir() -> Path:
    """Resolve the base data directory for Wisp.

    Returns:
        Path: ``$WISP_DATA_DIR`` when set, else the platformdirs user data
            path for ``"wisp"``.
    """
    env = os.environ.get("WISP_DATA_DIR")
    if env:
        return Path(env)
    return platformdirs.user_data_path("wisp")


def _config_dir() -> Path:
    """Resolve the base config directory for Wisp.

    Returns:
        Path: ``$WISP_CONFIG_DIR`` when set, else the platformdirs user config
            path for ``"wisp"``.
    """
    env = os.environ.get("WISP_CONFIG_DIR")
    if env:
        return Path(env)
    return platformdirs.user_config_path("wisp")


def _log_dir() -> Path:
    """Resolve the base log directory for Wisp.

    Returns:
        Path: ``$WISP_LOG_DIR`` when set, else the platformdirs user log path
            for ``"wisp"``.
    """
    env = os.environ.get("WISP_LOG_DIR")
    if env:
        return Path(env)
    return platformdirs.user_log_path("wisp")


def _script_path() -> Path:
    """Resolve the WireGuard server installer script path from package data.

    Wheels install unzipped, so the packaged file is a real filesystem path
    and a plain ``Path`` is safe here (no context-managed ``as_file`` needed).

    Returns:
        Path: Path to the packaged ``wisp/data/wireguard-server-install.sh``.
    """
    return Path(str(files("wisp") / "data" / "wireguard-server-install.sh"))


def _watchdog_script_path() -> Path:
    """Resolve the session watchdog script path from package data.

    Wheels install unzipped, so the packaged file is a real filesystem path
    and a plain ``Path`` is safe here (no context-managed ``as_file`` needed).

    Returns:
        Path: Path to the packaged ``wisp/data/wisp-watchdog.sh``.
    """
    return Path(str(files("wisp") / "data" / "wisp-watchdog.sh"))


WISP_DATA_DIR: Path = _data_dir()
WIREGUARD_KEYS_DIR: Path = WISP_DATA_DIR / "keys"
WIREGUARD_KEY_PATH: Path = WIREGUARD_KEYS_DIR / "wireguard-key.pem"
WIREGUARD_CLIENT_CONF_PATH: Path = WISP_DATA_DIR / "wireguard-confs" / "wg0-client.conf"

WISP_CONFIG_DIR: Path = _config_dir()
WISP_USER_CONFIG_PATH: Path = WISP_CONFIG_DIR / "wisp.toml"

# Marker describing the currently active Wisp session. Written when a tunnel
# comes up and removed once its resources are destroyed, so a stale file means
# a previous run died before cleaning up (see wisp.session).
WISP_ACTIVE_SESSION_FILE_NAME: str = "active-session.json"
WISP_ACTIVE_SESSION_PATH: Path = WISP_CONFIG_DIR / WISP_ACTIVE_SESSION_FILE_NAME

WISP_LOG_DIR: Path = _log_dir()

# WireGuard server installer script (packaged data)
WIREGUARD_SCRIPT_PATH: Path = _script_path()

# Session watchdog script (packaged data)
WATCHDOG_SCRIPT_PATH: Path = _watchdog_script_path()


def ensure_data_dir() -> Path:
    """Ensure the Wisp data directory exists with mode 0700.

    Returns:
        Path: The data directory path.
    """
    WISP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(WISP_DATA_DIR, 0o700)
    except OSError:
        pass

    return WISP_DATA_DIR


def ensure_keys_dir() -> Path:
    """Ensure the WireGuard keys directory exists.

    Returns:
        Path: The keys directory path.
    """
    ensure_data_dir()
    WIREGUARD_KEYS_DIR.mkdir(parents=True, exist_ok=True)
    return WIREGUARD_KEYS_DIR


def ensure_client_conf_dir() -> Path:
    """Ensure the WireGuard client configs directory exists.

    Returns:
        Path: The client configs directory path.
    """
    wireguard_confs_dir = WIREGUARD_CLIENT_CONF_PATH.parent
    ensure_data_dir()
    wireguard_confs_dir.mkdir(parents=True, exist_ok=True)
    return wireguard_confs_dir


def ensure_config_dir() -> Path:
    """Ensure the Wisp config directory exists.

    Returns:
        Path: The config directory path.
    """
    WISP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return WISP_CONFIG_DIR


def ensure_log_dir() -> Path:
    """Ensure the Wisp log directory exists.

    Returns:
        Path: The log directory path.
    """
    WISP_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return WISP_LOG_DIR


# WireGuard defaults (non-interactive installer)
WIREGUARD_INTERFACE: str = "wg0"
WIREGUARD_IPV4: str = "10.66.66.1"
WIREGUARD_IPV6: str = "fd42:42:42::1"
WIREGUARD_DNS1: str = "1.1.1.1"
WIREGUARD_DNS2: str = "1.0.0.1"
WIREGUARD_CLIENT_NAME: str = ""
