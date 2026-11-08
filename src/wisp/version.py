"""Resolve the installed Wisp version from distribution metadata."""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION_NAME = "wisp-vpn"


def get_version() -> str:
    """Return the version recorded for the installed ``wisp-vpn`` distribution.

    The value comes from the package metadata the build backend writes from
    ``pyproject.toml``, so it always matches the installed build without
    parsing files at runtime. This works for a PyPI install, an editable
    source install, and the daemon's own venv alike.

    Returns:
        str: The installed version, or ``"unknown"`` when the distribution
        metadata is unavailable (for example, running from a bare checkout
        that was never installed).
    """
    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "unknown"
