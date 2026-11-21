from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from wisp.config.constants import WINDOWS_SERVICE_NAME

if TYPE_CHECKING:
    from wisp.daemon.transport import WindowsNamedPipeTransport


def _configure_log_directory() -> None:
    """Select a writable log directory before importing the daemon."""
    configured = os.environ.get("WISP_LOG_DIR")
    preferred = (
        Path(configured)
        if configured
        else Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "wisp" / "logs"
    )
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        with (preferred / "wisp.log").open("a", encoding="utf-8"):
            pass
    except OSError:
        preferred = Path(tempfile.gettempdir()) / "wisp" / "logs"
        preferred.mkdir(parents=True, exist_ok=True)
    os.environ["WISP_LOG_DIR"] = str(preferred)


# An SCM service has no console: stderr is lost, so the file handler is this
# process's only usable sink. Configure it before the daemon logger imports.
_configure_log_directory()

import servicemanager  # noqa: E402  # pyright: ignore[reportMissingModuleSource]
import win32service  # noqa: E402  # pyright: ignore[reportMissingModuleSource]
import win32serviceutil  # noqa: E402  # pyright: ignore[reportMissingModuleSource]


class WispService(win32serviceutil.ServiceFramework):
    """Windows Service wrapper for the Wisp daemon."""

    _svc_name_ = WINDOWS_SERVICE_NAME
    _svc_display_name_ = "Wisp Daemon Service"
    _svc_description_ = "Privileged daemon for managing the Wisp VPN on Windows."
    _exe_name_ = str(Path(sys.executable).with_name("pythonservice.exe"))

    def __init__(self, args):
        super().__init__(args)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task | None = None
        self._transport: WindowsNamedPipeTransport | None = None

    def SvcStop(self):
        """Handle a stop request from the Windows Service Control Manager."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)

        if self._loop is not None and self._transport is not None:
            self._loop.call_soon_threadsafe(self._transport.stop)

    def SvcDoRun(self):
        """Run the Wisp daemon after pywin32 reports the service as running."""
        servicemanager.LogInfoMsg(f"{self._svc_name_} service started.")

        try:
            from wisp.daemon.server import main
            from wisp.daemon.transport import WindowsNamedPipeTransport

            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._transport = WindowsNamedPipeTransport()
            self._main_task = self._loop.create_task(main(transport=self._transport))

            self._loop.run_until_complete(self._main_task)

        except Exception:
            failure = f"{self._svc_name_} failed:\n{traceback.format_exc()}"
            servicemanager.LogErrorMsg(failure)
            # This sink also works when importing the daemon logger failed.
            try:
                log_path = Path(os.environ["WISP_LOG_DIR"]) / "service-startup.log"
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"[{datetime.now(timezone.utc).isoformat()}] {failure}\n"
                    )
            except OSError:
                pass
            raise

        finally:
            self._main_task = None
            self._transport = None

            if self._loop is not None:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.close()
                self._loop = None
                asyncio.set_event_loop(None)

        servicemanager.LogInfoMsg(f"{self._svc_name_} service stopped.")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(
        WispService, serviceClassString="wisp.daemon.windows_service.WispService"
    )
