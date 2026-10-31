# Architecture

## Overview

Wisp is a Python application (package `wisp`, source under `src/wisp/`) that
orchestrates three cooperating pieces:

1. A **frontend** — an interactive Textual TUI or argparse-based CLI — that the
   user drives.
2. A **cloud provider layer** that uses the Pulumi Automation API to provision
   and destroy infrastructure, and configures the remote host over
   SSH/SFTP (Paramiko).
3. A **privileged local daemon** that manages the client-side WireGuard
   interface without requiring the user to run the whole application as root.

## Package layout

```console
src/wisp/
├── main.py             # Entry point; argparse CLI + TUI dispatch
├── schemas.py          # Shared TypedDicts (SSH/WireGuard context for the deploy)
├── session.py          # Session lifecycle: SessionGuard, active-session marker, teardown
├── cli/                # Textual TUI
│   ├── app.py          # WispApp (global styles, screen registration, shutdown wiring)
│   ├── state.py        # AppState (in-memory session state)
│   ├── __main__.py     # `python -m wisp.cli` launcher
│   └── screens/        # MainMenu, Config, Deploy, Progress, Tunnel, Shutdown screens
├── config/             # Configuration
│   ├── settings.py     # TOML loading, WispConfig dataclass, per-provider defaults
│   ├── credentials.py  # Per-provider credential resolution (AWS/OCI/GCP/VPS)
│   └── constants.py    # Paths, tags, WireGuard defaults
├── providers/          # Cloud provider abstraction
│   ├── base.py         # BaseProvider ABC + result TypedDicts
│   ├── pulumi_base.py  # PulumiProvider: shared deploy/destroy flow
│   ├── aws/            # AWS implementation (provider, pulumi program, constants)
│   ├── oci/            # OCI implementation (provider, pulumi program, constants)
│   ├── gcp/            # GCP implementation (provider, pulumi program, constants)
│   └── vps.py          # Custom VPS (bring-your-own-server): SSH-only, no Pulumi
├── wireguard/          # Client/server WireGuard orchestration
│   ├── local_client.py # Talks to the daemon over the Unix socket / Named Pipe
│   └── remote_server.py# Configures the remote server via Paramiko (SSH/SFTP)
├── daemon/             # Privileged local daemon
│   ├── server.py       # asyncio server (runs as root/SYSTEM)
│   ├── transport.py    # Unix socket (Linux/macOS) + Named Pipe (Windows)
│   ├── adapter.py      # Platform-specific privileged operations
│   ├── protocol.py     # Request/Response wire format + socket path
│   └── windows_service.py # Windows Service wrapper (pywin32)
└── utils/              # Helpers: logging, platform detection, RNG, public IP,
                        # Pulumi helpers (gc), session TTL watchdog
```

Supporting non-Python assets at the repository root:

```console
scripts/    *.sh                     # WireGuard client install + shared shell utils
packaging/  wisp.service, wisp.socket# systemd units for the daemon (Linux)
setup.ps1                            # Native Windows installer (service + WireGuard exe)
```

Runtime artifacts (private key, client config, logs) no longer live in the repo:
they are resolved per user via `platformdirs` (see
[Configuration](./configuration.md)). The server installer and in-VM session
watchdog ship as package data at `src/wisp/data/wireguard-server-install.sh` and
`src/wisp/data/wisp-watchdog.sh`.

## Runtime components and boundaries

| Component | Privilege | Where it runs | Responsibility |
| ----------- | ----------- | --------------- | ---------------- |
| CLI / TUI | user | local | Collect config, trigger deploy/destroy, show progress |
| Provider | user (uses cloud creds) | local | Pulumi up/destroy, run remote configuration over SSH/SFTP |
| Pulumi program | user | local process | Declare instance, network, security rules, TLS key pair |
| Remote installer (SSH) | remote root (via SSH) | remote VM | Install & configure WireGuard server, fetch client conf |
| wisp daemon | root / SYSTEM | local (systemd on Linux, Windows Service on Windows) | `wg-quick up/down wg0` on the local machine |

The **trust boundary** worth noting: the unprivileged frontend never runs
`wg-quick` directly. Instead it sends a request to the daemon over
`/run/wisp.sock` (Linux) or `\\.\pipe\wisp` (Windows). The socket/pipe is
restricted to root and the `wisp` group (`0660 root:wisp` on Linux;
SIDs on Windows), so any user in the `wisp` group can control the local tunnel,
but only the privileged daemon actually mutates the WireGuard config and brings
the interface up.

## End-to-end data flow (deploy)

```text
User → TUI/CLI
  → Progress screen: write active-session marker (with TTL metadata) + arm SessionGuard
      (+ schedule local TTL watchdog if max_session_ttl_minutes > 0)
  → Provider.deploy_vm(region, config)
      → Pulumi Automation: create_or_select_stack(program).up()     [aws/oci/gcp]
          program = create_ec2_instance / create_oci_instance / create_gcp_instance(...)
            - AMI/image lookup (most recent Ubuntu image)
            - security rules (UDP wg port + TCP 22)
            - tls.PrivateKey (RSA 4096) → SSH key pair
            - instance (AWS terminate on shutdown, GCP SPOT delete on terminate)
            - exports: instance_id, public/private ip, private_key, wg_port, ssh_user
      → wait `config.vm_boot_timeout` seconds for SSH/boot (default 60)
      → write private key to WISP_DATA_DIR/keys/wireguard-key.pem (0600 / icacls on Windows)
      → configure_remote_server(context)
            → Paramiko SSH/SFTP: upload + run packaged wireguard-server-install.sh
              (non-interactive, driven by env vars)
            - fetches wg0-client-*.conf → WISP_DATA_DIR/wireguard-confs/wg0-client.conf
            - appends PersistentKeepalive = 25 if missing
            - install_session_watchdog: uploads wisp-watchdog.sh, writes /etc/wisp/watchdog-<nic>.conf,
              arms wisp-watchdog@<nic>.timer (ticks every 60s)
      → connect_wireguard_client(conf_text)
            → daemon: write config (0600) + `wg-quick up wg0` (Linux) or Windows service (start=demand)
  → returns DeployVMResult(instance_id, public_ip, private_ip, wireguard_port)

  VPS provider: no Pulumi, no boot wait — SSH straight into the user's server
  and run the same installer + watchdog (mode=vps, server-side interface named wisp-<hex>).
```

## End-to-end data flow (destroy)

```text
User → TUI/CLI
  → Provider.delete_vm(region)
      → disconnect_wireguard_client()  → daemon: `wg-quick down wg0`
      → Pulumi Automation: stack.cancel() (no-op if idle) → stack.destroy()
      → remove local artifacts: WISP_DATA_DIR/keys/wireguard-key.pem,
        WISP_DATA_DIR/wireguard-confs/wg0-client.conf
  → returns count of deleted resources

  VPS provider: additionally SSHes in and disarms the in-VM watchdog (disables
  timer, deletes /etc/wisp/watchdog-<nic>.conf, cleans up units/script if last
  session), then tears down every /etc/wireguard/wisp-*.conf (wg-quick down +
  disable + rm), then removes the packages recorded in
  /etc/wisp/installed-packages (only those Wisp added, and only while no
  other *.conf remains; iptables/resolvconf are never removed). Non-wisp
  configs are never touched. Then cancels the local TTL watchdog and clears
  the marker.
```

## Session lifecycle and watchdog layers

Because an active session represents billable infrastructure, Wisp protects against
abandoned resources with two cooperating watchdog layers:

1. **In-VM watchdog (primary)**:
   Installed autonomously on the remote host during deployment (`/usr/local/bin/wisp-watchdog`,
   `/etc/wisp/watchdog-<nic>.conf`, `wisp-watchdog@.service`, and `wisp-watchdog@.timer`
   firing every 60 seconds). Because it executes inside the remote VM, it functions as
   a dead man's switch that survives client power loss, system suspend, or application crashes.
   - **Triggers**: Either a hard session TTL deadline (`DEADLINE_EPOCH`, calculated against
     the server clock at install time to avoid clock skew; 0 disables) or an idle timeout
     (`DEFAULT_WATCHDOG_IDLE_SECONDS = 900`, 15 minutes with no WireGuard handshake; if no peer
     has ever handshook, measured from session deploy time).
   - **Cattle vs pets rule**:
     - **Cloud VMs (`mode=cloud`)**: Treated as disposable cattle. The watchdog executes
       `systemctl poweroff`. AWS EC2 instances are configured with
       `instance_initiated_shutdown_behavior="terminate"` (terminating the VM and deleting
       the root EBS volume); GCP SPOT instances have `instance_termination_action="DELETE"`;
       OCI compute halts, stopping compute billing (boot volume persists until stack destruction).
     - **Custom VPS (`mode=vps`)**: Treated as persistent pets hosting other user workloads.
       The watchdog **never** powers off or reboots a VPS. Instead, it surgically tears down
       only Wisp's own interface (`wg-quick down`), its `/etc/wireguard/wisp-*.conf`, client
       configs, its owned `/etc/wireguard/params`, and its own systemd units.
   - **Keepalive**: `PersistentKeepalive = 25` is automatically added to client configurations
     so idle connections emit regular handshakes, avoiding false idle triggers.

2. **Local watchdog (secondary)**:
   An OS-level one-shot scheduled on the user's machine (`systemd-run --user` on Linux,
   `schtasks` on Windows) via `utils/watchdog.py`. When `max_session_ttl_minutes > 0`, it
   executes `wisp destroy <provider> -r <region>` locally upon TTL expiration.

### Client network resilience and recovery

- **Windows start type**: WireGuard tunnel services on Windows are explicitly set to manual
  start (`sc.exe config WireGuardTunnel$wg0 start= demand`) so that a reboot does not restore a
  stale default route pointing at a terminated server.
- **Daemon startup hygiene**: On startup before accepting client commands, the daemon executes
  `reset_stale_tunnel`. It disconnects any interface that is up but has never handshook or
  whose latest handshake is older than 180 seconds.
- **Local recovery commands**: `wisp disconnect` provides an immediate local panic switch to
  tear down the local interface without requiring cloud credentials or contacting remote APIs.
  `wisp connect` re-attaches the local client using the stored configuration.
- **Session marker resilience**: `active-session.json` records `ttl_minutes` and `expires_at`,
  allowing `ActiveSession.is_resumable()` to detect live sessions across restarts.
- **Startup adoption**: when the marker belongs to a process that is no longer alive *and* the
  daemon reports an interface with a handshake younger than `STALE_TUNNEL_SECONDS` (180 s), the
  TUI adopts the tunnel and opens the tunnel view instead of the main menu. The existing marker
  is never rewritten (that would reset `started_at`/`expires_at` and silently extend the
  session) and the OS one-shot is never rescheduled. A marker whose owner process is still
  alive is never hijacked.
- **Remote death detection**: the tunnel view classifies the daemon reply through
  `health_state()` (`live`, `waiting`, `local_down`, `remote_gone`) using
  `Response.handshake_age_seconds`. A local interface that is down says nothing about the
  remote end (`wisp disconnect` is local), so only a stale handshake moves the TUI to the
  session-ended screen; that screen disarms the guard, since the resources it would destroy are
  already gone.
- **External teardown visibility**: a destroy writes `teardown-notice.json` for its duration.
  The tunnel view polls it and switches to a "destroying resources" screen when a *different*
  process is tearing the session down, then returns to the menu once the notice clears.

### Concurrency

Wisp does **not** implement its own cross-process lock. Pulumi already serializes updates: its
backend locks a stack for the duration of an `up`/`refresh`/`destroy`, and a second concurrent
update raises `pulumi.automation.errors.ConcurrentUpdateError` instead of corrupting state.
Wisp's contribution is error presentation: `describe_pulumi_error()` unwraps the wrapped
`command_result` output and names the concurrent-update case explicitly, so the CLI and the
TUI error box show something actionable instead of `code: 1`.

`teardown-notice.json` is observability, not mutual exclusion: nothing waits on it and its
absence never blocks an operation.

## Provider abstraction

All providers implement `BaseProvider` (`providers/base.py`); the shared
deploy/destroy flow for Pulumi-based providers lives in `PulumiProvider`
(`providers/pulumi_base.py`), so each concrete provider only declares its own
Pulumi program and provider-specific logic:

- `get_available_regions() -> Sequence[str]`
- `deploy_vm(region, force_current_ip=False, config=None, on_progress=None) -> DeployVMResult`
- `delete_vm(region, on_progress=None) -> int`

`ProviderEnum` and `PROVIDERS_MAP` in `providers/__init__.py` map a provider
name (`"aws"`, `"oci"`, `"gcp"`, `"vps"`) to its concrete class. Adding a new
provider means
implementing `BaseProvider` (or extending `PulumiProvider`) and registering it
in `PROVIDERS_MAP`. See [Contributing](./contributing.md).

> **Note:** As of this writing, Wisp ships with support for `aws`, `oci`,
> `gcp`, and `vps`. The Pulumi-based providers share `PulumiProvider`; the
> `vps` provider implements `BaseProvider` directly (SSH-only, no Pulumi).
> The provider layer is designed to be extensible — new providers can be added
> by implementing `BaseProvider` (or extending `PulumiProvider`) and
> registering them in `PROVIDERS_MAP`
> ([`src/wisp/providers/__init__.py`](../src/wisp/providers/__init__.py)).
> For the current list, see `ProviderEnum`
> ([`src/wisp/providers/base.py`](../src/wisp/providers/base.py)).

The `on_progress` callback (`Callable[[str, float | None], None]`) lets the TUI's
`ProgressScreen` render live progress; in pure CLI mode it is `None` and a `tqdm`
progress bar is used for the boot wait instead.

## Operating system differences

- **Linux**: `wisp daemon install` installs the daemon (systemd socket activation
  via `wisp.socket`/`wisp.service`) and `wg-quick` manages the local WireGuard
  interface. The daemon transports on Unix domain sockets (`/run/wisp.sock`).
  Sensitive files are secured with `0600` permissions.
- **Windows**: `setup.ps1` installs a native Windows Service
  (`wisp.daemon.windows_service`, pywin32), the WireGuard Windows installer, and
  configures Named Pipes (`\\.\pipe\wisp`) for daemon communication. No systemd
  or `wg-quick`; the Windows service and its adapter manage the interface.
  Sensitive files are secured with `icacls` using well-known SIDs.

> **Note:** Linux and Windows are fully supported. The macOS adapter is not
> implemented yet — see `MacOSAdapter`
> ([`src/wisp/daemon/adapter.py`](../src/wisp/daemon/adapter.py)) for the
> current platform state.
