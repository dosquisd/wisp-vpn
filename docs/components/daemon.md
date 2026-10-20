# `wisp.daemon`

The privileged local daemon that controls the client-side WireGuard interface.
Runs as root under systemd (Linux) or as a native Windows Service (pywin32);
the unprivileged frontend talks to it over a Unix socket (Linux) or Named Pipe
(Windows). See also [security model](../security.md).

## Installation

`wisp daemon install` / `wisp daemon uninstall`
([`wisp/daemon/install.py`](../../src/wisp/daemon/install.py), wired from
`wisp.main`) installs, refreshes, and removes the daemon without running
`setup.sh`/`setup.ps1`:

- **Linux**: the command first builds a **root-owned venv** at
  `/usr/local/lib/wisp/venv` (base interpreter picked from a candidate
  cascade — `python3.14`, `python3.13`, `python3.12`, `python3` — where the
  first candidate that survives *create venv → `pip install wisp-vpn` →
  `import wisp.daemon.server`* wins) and only then touches systemd. The units
  ship as package data (`wisp/data/wisp.socket` verbatim,
  `wisp/data/wisp.service.tpl` with an `{python_executable}` placeholder) and
  are rendered with that venv's interpreter. The reason for the dedicated
  venv: a root service must not execute user-mutable code — pointing
  `ExecStart` at the invoking user's `uv tool` venv let that venv invalidate
  itself under the user and systemd failed the service with `203/EXEC`
  (live-verified). The command also creates the `wisp` group and adds the
  invoking sudo user to it (re-login required). `uninstall` removes the units
  together with the root-owned runtime directory.
- **Windows**: dispatches to `wisp.daemon.windows_service`
  (`install` / `--startup auto update` / `remove`).
- Any other platform: a clear "not supported" error.

## Logging

The daemon's logs live in one place per platform:

- **Linux**: `journalctl -u wisp.service` — the daemon logs to stderr at
  `DEBUG` and journald captures it; no log file, and no filesystem paths are
  touched.
- **Windows**: `C:\ProgramData\wisp\logs\wisp.log` — an SCM service has no
  console, so the service wrapper sets `WISP_LOG_DIR` process-locally before
  importing the server and the daemon uses the regular rotating file handler.

## `daemon/protocol.py`

Wire protocol shared by the daemon and its local client:

- `ActionEnum(StrEnum)`: `CONNECT`, `DISCONNECT`, `STATUS`.
- `Request` dataclass: `action: ActionEnum`, `config_content: str | None`
  (only for `connect`). `encode()`/`decode()` use newline-terminated JSON.
- `Response` dataclass: `ok: bool`, `message: str`, with matching
  `encode()`/`decode()`.

## `daemon/transport.py`

Transport implementations selected by `get_transport()`:

- `UnixSocketTransport` (Linux) — asyncio Unix-socket server at
  `SOCKET_PATH = "/run/wisp.sock"`. Under systemd socket activation
  (`LISTEN_FDS` set) the listening socket is adopted from fd 3; for local
  development the socket is created directly. `send()` connects, sends one
  request, reads one response line; raises a descriptive `PermissionError` on
  connection failure.
- `WindowsNamedPipeTransport` (Windows) — duplex Named Pipe at
  `PIPE_NAME = r"\\.\pipe\wisp"`. Builds a security descriptor granting full
  control to SYSTEM and built-in Administrators, and read/write to the `wisp`
  group (well-known SIDs, so it works on localized Windows installations).
  Serves one client per pipe instance in a loop; `stop()` wakes a pending
  `ConnectNamedPipe` call.
- `PipeStreamReader` / `PipeStreamWriter` — minimal asyncio-compatible
  reader/writer wrappers over the Win32 pipe handles.

## `daemon/adapter.py`

Platform-specific privileged operations, selected by `get_adapter()`:

- `BaseAdapter(ABC)` — abstract `connect(config_content)`, `disconnect()`,
  `status()`, plus a `WG_CONF_PATH: Path` attribute and a `_run(cmd)` helper.
- `LinuxAdapter` — `WG_CONF_PATH = /etc/wireguard/wg0.conf`. `connect` writes
  the config (`0600`) and runs `wg-quick up wg0`; `disconnect` runs
  `wg-quick down wg0`; `status` returns `wg show wg0` output.
- `WindowsAdapter` — `WG_CONF_PATH = /ProgramData/wisp/wireguard/wg0.conf`.
  Derives the tunnel name from the config stem and the tunnel service name
  (`WireGuardTunnel$<name>`). `connect` removes any leftover tunnel from a
  previous session, writes the config (secured via `secure_file`/icacls), and
  runs `wireguard /installtunnelservice`; `disconnect` uses
  `wireguard /uninstalltunnelservice`.
- `MacOSAdapter` — **not implemented yet**; all operations raise
  `NotImplementedError`.

## `daemon/server.py`

Async request handling:

- `handle_connect(config_content, adapter)` — writes the WireGuard config via
  the adapter (`0600`/icacls) and brings the tunnel up. Returns
  `Response(ok=True, "connected")` or the command's stderr on
  `CalledProcessError`.
- `handle_disconnect(adapter)` — brings the tunnel down.
- `handle_status(adapter)` — returns the tunnel status output.
- `handle_client(reader, writer)` — reads one `Request`, dispatches by action
  with the platform adapter, writes one `Response`. Unknown actions and
  exceptions yield `ok=False`.
- `main(transport=None)` — starts the transport server (a
  `UnixSocketTransport` by default, or the injected transport — the Windows
  Service injects a `WindowsNamedPipeTransport`) and serves forever.

Run standalone with:

```bash
python -m wisp.daemon.server
```

(under systemd this is `wisp.service`, requiring `wisp.socket`).

## `daemon/windows_service.py`

`WispService(win32serviceutil.ServiceFramework)` — Windows Service wrapper:

- `_svc_name_ = "WispService"`, display name "Wisp Daemon Service".
- `SvcRun` starts an asyncio event loop and runs `main(transport=...)` with a
  `WindowsNamedPipeTransport`; `SvcStop` signals the transport to stop and
  drains/closes pending tasks.
- Install/update/remove via the pywin32 command line:

```bash
python -m wisp.daemon.windows_service install
python -m wisp.daemon.windows_service --startup auto update
python -m wisp.daemon.windows_service remove
```
