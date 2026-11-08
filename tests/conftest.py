"""Shared Textual test harness for the Wisp TUI.

Two things need neutralising before a screen can be laid out:

- `run_test` delivers `Mount` more than once, so `WispApp.on_mount` would install
  the screens twice and raise `ScreenError`. Textual dispatches handlers by
  walking the *class* MRO (`MessagePump._get_dispatch_methods`), so the guard has
  to live in a patched class method — an instance attribute never sees it.
- `DeployScreen.fetch_live_regions` calls the boto3 / OCI / GCP region APIs, so
  it is stubbed to keep layout deterministic and offline.

The runtime directory environment overrides are set *before* importing the app
(and thus before `wisp.config.constants` resolves paths), so tests never touch
the developer's real data/config/log directories.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Hermetic test roots — must run before the wisp imports below resolve paths.
_test_root = Path(tempfile.mkdtemp(prefix="wisp-tests-"))
for _var, _sub in (
    ("WISP_DATA_DIR", "data"),
    ("WISP_CONFIG_DIR", "config"),
    ("WISP_LOG_DIR", "log"),
):
    _dir = _test_root / _sub
    _dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(_var, str(_dir))

import pytest  # noqa: E402

from wisp.tui.app import WispApp  # noqa: E402
from wisp.tui.screens import ConfigScreen, DeployScreen, MainMenuScreen  # noqa: E402

SCREENS = {
    "main_menu": MainMenuScreen,
    "config": ConfigScreen,
    "deploy": DeployScreen,
}


def _install_once(self: WispApp) -> None:
    """Idempotent replacement for `WispApp.on_mount`: install screens, push menu."""
    if getattr(self, "_wisp_ready", False):
        return
    self._wisp_ready = True
    for name, cls in SCREENS.items():
        self.install_screen(cls(), name=name)
    self.push_screen("main_menu")


@pytest.fixture
def wisp_app(monkeypatch):
    """Return a `WispApp` with its screens installed and deploy inert."""
    monkeypatch.setattr(DeployScreen, "fetch_live_regions", lambda self: None)
    monkeypatch.setattr(WispApp, "on_mount", _install_once)
    return WispApp()
