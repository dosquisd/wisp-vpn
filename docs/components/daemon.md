# `wisp.daemon`

The privileged local daemon that controls the client-side WireGuard interface.
Runs as root under systemd (Linux) or as a native Windows Service (pywin32);
the unprivileged frontend talks to it over a Unix socket (Linux) or Named Pipe
(Windows). See also [security model](../security.md).

## Installation

`wisp daemon install` / `wisp daemon uninstall`
([`wisp/daemon/install.py`](../../src/wisp/daemon/install.py), wired from
`wisp.main`) installs, refreshes, and removes the daemon on its own — on Linux
no setup script is involved, and on Windows `setup.ps1` still covers the
service install:

- **Linux**: the command first builds a **root-owned venv** at
  `/usr/local/lib/wisp/venv` (base interpreter picked from a candidate
  cascade — `python3.14`, `python3.13`, `python3.12`, `python3` — where the
  first candidate that survives *create venv → `pip install wisp-vpn` →
  `import wisp.daemon.server`* wins) and only then touches systemd. What gets
  installed is not always the PyPI release: `_daemon_install_target()` reads
  the running CLI's PEP 610 `direct_url.json`, and when the CLI is an
  editable or local install the daemon is built from **that checkout** (staged
  into a temp directory, so a `sudo` build never leaves root-owned `build/` or
  `*.egg-info` in the developer's repo). Without this, a developer's CLI and
  the privileged daemon silently diverge: the CLI runs the working tree while
  the daemon keeps executing the last published release — new daemon code
  never runs, and the symptom shows up far from the cause. The units
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
- `Response` dataclass: `ok: bool`, `message: str`,
  `handshake_age_seconds: int | None` (defaulted, so older clients and daemons
  stay compatible), with matching `encode()`/`decode()`. The age field is what
  lets the tunnel view tell "local interface down" apart from "remote end gone".

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
  `status()`, plus a `WG_CONF_PATH: Path` attribute, a `_run(cmd)` helper, and
  `latest_handshake_epoch() -> int | None` (defaults to `None`).
- `_parse_latest_handshakes(output: str) -> int | None` — parses `wg show <iface> latest-handshakes`
  output into the maximum epoch timestamp across peers, `0` if peers exist but have
  never handshook, or `None` if unparseable/empty.
- `LinuxAdapter` — `WG_CONF_PATH = /etc/wireguard/wg0.conf`. `connect` writes
  the config (`0600`) and runs `wg-quick up wg0`; `disconnect` runs
  `wg-quick down wg0`; `status` returns `wg show wg0` output;
  `latest_handshake_epoch()` queries `wg show wg0 latest-handshakes`.
- `WindowsAdapter` — `WG_CONF_PATH = /ProgramData/wisp/wireguard/wg0.conf`.
  Derives the tunnel name from the config stem and the tunnel service name
  (`WireGuardTunnel$<name>`). `connect` removes any leftover tunnel from a
  previous session, writes the config (secured via `secure_file`/icacls),
  runs `wireguard /installtunnelservice`, and then configures manual start:
  `sc.exe config WireGuardTunnel$<name> start= demand`. WireGuard for Windows
  defaults to `auto` start; setting `demand` ensures a workstation reboot
  does not resurrect a dead tunnel. `disconnect` uses
  `wireguard /uninstalltunnelservice`; `latest_handshake_epoch()` queries
  `wg show <tunnel_name> latest-handshakes`.
- `MacOSAdapter` — **not implemented yet**; all operations raise
  `NotImplementedError`.

## `daemon/server.py`

Constants and async request handling:

- `STALE_TUNNEL_SECONDS = 180` — maximum allowable handshake age during startup
  staleness detection.
- `reset_stale_tunnel(adapter, now=None) -> bool` — startup hygiene function.
  The daemon runs as root/SYSTEM and cannot inspect the user's session marker,
  so WireGuard handshake recency is used as the cross-user signal. If the
  interface is up but has never completed a handshake (`handshake == 0`), or if
  the latest handshake is older than 180s, `adapter.disconnect()` is called to
  tear down the leftover tunnel and prevent routing black holes. Healthy live
  tunnels (kept fresh via `PersistentKeepalive = 25`) are left undisturbed.
- `handle_connect(config_content, adapter)` — writes the WireGuard config via
  the adapter (`0600`/icacls) and brings the tunnel up. Returns
  `Response(ok=True, "connected")` or the command's stderr on
  `CalledProcessError`.
- `handle_disconnect(adapter)` — brings the tunnel down.
- `handle_status(adapter)` — returns the tunnel status output, plus
  `handshake_age_seconds` computed from `adapter.latest_handshake_epoch()`
  (`None` when the interface is down or has no peer).
- `handle_client(reader, writer)` — reads one `Request`, dispatches by action
  with the platform adapter, writes one `Response`. Unknown actions and
  exceptions yield `ok=False`.
- `main(transport=None)` — starts the transport server. On boot, calls
  `await reset_stale_tunnel(transport.adapter)` before serving connections.

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
