import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.daemon.transport import WindowsNamedPipeTransport

# An SCM service has no console: stderr is lost, so the file handler is this
# process's only usable sink. Point it at the install's log dir via a
# process-local default — never machine-wide. Must run before the server
# import configures the logger.
os.environ.setdefault(
    "WISP_LOG_DIR",
    str(Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "wisp" / "logs"),
)

import servicemanager  # noqa: E402  # pyright: ignore[reportMissingModuleSource]
import win32service  # noqa: E402  # pyright: ignore[reportMissingModuleSource]
import win32serviceutil  # noqa: E402  # pyright: ignore[reportMissingModuleSource]


class WispService(win32serviceutil.ServiceFramework):
    """Windows Service wrapper for the Wisp daemon."""

    _svc_name_ = "WispService"
    _svc_display_name_ = "Wisp Daemon Service"
    _svc_description_ = "Privileged daemon for managing the Wisp VPN on Windows."
    _exe_name_ = str(Path(sys.exec_prefix) / "pythonservice.exe")

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

        from wisp.daemon.server import main
        from wisp.daemon.transport import WindowsNamedPipeTransport

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._transport = WindowsNamedPipeTransport()

        try:
            self._main_task = self._loop.create_task(main(transport=self._transport))

            self._loop.run_until_complete(self._main_task)

        except Exception as exc:
            servicemanager.LogErrorMsg(f"{self._svc_name_} failed: {exc}")
            raise

        finally:
            self._main_task = None
            self._transport = None

            pending = asyncio.all_tasks(self._loop)

            for task in pending:
                task.cancel()

            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )

            self._loop.close()
            self._loop = None

        servicemanager.LogInfoMsg(f"{self._svc_name_} service stopped.")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(WispService)
