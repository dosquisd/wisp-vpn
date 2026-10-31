# Security model

## Privilege separation via the daemon

The client-side WireGuard interface requires root to bring up/down. Rather than
running the whole application as root, Wisp splits privileges:

- The **unprivileged frontend** (CLI/TUI) runs as your user.
- A **privileged daemon** (`wisp.service`, socket-activated by `wisp.socket` on
  Linux; native Windows Service via pywin32 on Windows) performs the privileged
  operations (`wg-quick up/down`, writing the WireGuard config).

They communicate over a Unix domain socket at `/run/wisp.sock` (Linux) or
a Named Pipe (`\\.\pipe\wisp`) on Windows.

### Socket access control

From `packaging/wisp.socket` (Linux):

- `SocketMode=0660`
- `SocketUser=root`
- `SocketGroup=wisp`

Only root and members of the `wisp` group can connect. `wisp daemon install`
creates the `wisp` group and adds the installing user (`setup.ps1` does the
equivalent on Windows with a local group). Group membership requires a re-login
(or `newgrp wisp`) to take effect; until then, connecting to the daemon raises a
`PermissionError` with guidance.

On Windows, the Named Pipe security descriptor grants full control to SYSTEM
and built-in Administrators, and read/write to the `wisp` group — implemented
with well-known SIDs in `daemon/transport.py`.

### Protocol

`daemon/protocol.py` defines a minimal newline-delimited JSON protocol:

- `Request { action: connect|disconnect|status, config_content?: str }`
- `Response { ok: bool, message: str }`

The daemon (`daemon/server.py`) handles one request per connection and dispatches
to the platform adapter for `connect` / `disconnect` / `status`.

> **Security note — socket access is effectively root.** The daemon writes
> whatever `config_content` it receives to the WireGuard config file and
> brings the interface up. On Linux, `wg-quick` executes the `PreUp`,
> `PostUp`, `PreDown`, and `PostDown` directives from that file **as root**, so
> membership in the `wisp` group is equivalent to local root — not merely
> control over the WireGuard interface. The daemon mitigates the config
> channel: `validate_client_config()` (`daemon/server.py`) rejects client
> configs containing those script-hook directives before anything is written,
> on both Linux and Windows. A local attacker with `wisp` group membership
> still holds a privileged position — treat the group like you treat `docker`.

## Keys and secrets

- A fresh **RSA 4096 key pair** is generated per deployment via `pulumi-tls`. The
  private key is a Pulumi stack output.
- **Where the private key lives:** stack outputs are stored in the Pulumi
  state. By default Wisp uses the Pulumi Cloud backend (`pulumi login`), so the
  state — including this key — resides server-side on app.pulumi.com under
  your organization; with a local/file backend it stays on your disk. As of
  this fix, the key output is wrapped with `pulumi.Output.secret(...)`, so the
  state stores it **encrypted at rest** instead of plaintext (older stacks
  created before this change keep the plaintext copy in their state until the
  resource is replaced or the state is edited).
- The private key is written locally to `WISP_DATA_DIR/keys/wireguard-key.pem`
  with mode `0600` (Linux) or secured via `icacls` with well-known SIDs
  (Windows), and used as the SSH key for the remote server configuration.
- On destroy, the key file and client config are removed.
- The key and client config live under the per-user data directory
  (`WISP_DATA_DIR`, mode `0700` on Linux) — outside the repository — so secrets
  are never committed.
- **Custom VPS uses YOUR key, not a generated one**: `ssh_key_path` is used
  as-is to SSH into your server — Wisp never generates, stores, or deletes
  it. Password authentication is not supported.

## The session watchdogs

Wisp uses two autonomous watchdog layers to bound the financial risk of abandoned
infrastructure:

1. **In-VM watchdog (remote root)**:
   Runs as root inside the remote server managed by systemd (`wisp-watchdog@<nic>.timer`
   and `/usr/local/bin/wisp-watchdog`). It acts as a dead man's switch that evaluates
   two conditions:
   - **Hard TTL**: Evaluates `DEADLINE_EPOCH` computed at deploy time against the remote
     clock; when exceeded, teardown triggers immediately.
   - **Idle handshake check**: Checks `wg show <nic> latest-handshakes`. If no handshake
     has occurred for 900 seconds (15 minutes), teardown triggers.
   - **VPS safety boundary**: On cloud VMs (`mode=cloud`), the watchdog executes
     `systemctl poweroff`. In AWS (`instance_initiated_shutdown_behavior="terminate"`)
     and GCP (`instance_termination_action="DELETE"`), this halts and terminates the VM.
     On a **Custom VPS (`mode=vps`)**, the watchdog **never powers off or halts the server**.
     It only tears down Wisp's specific interface (`wg-quick down`), its configuration
     files, and its own systemd units, leaving all unrelated services and host operations
     untouched.

2. **Local watchdog (local user)**:
   The optional local watchdog (`[general].max_session_ttl_minutes`, default `60`)
   schedules an OS-level one-shot (`systemd-run --user` on Linux, `schtasks` on
   Windows) that runs `wisp destroy <provider> -r <region>` when the TTL expires.
   The command runs under your local user account with your cloud credentials and the
   same permission model as a manual destroy — it is exactly `wisp destroy`, not
   a privileged background service. Scheduling is idempotent and manual teardown
   cancels it.

## Local network fail-safes and panic disconnect

- **Panic disconnect without cloud credentials**: `wisp disconnect` is a local-only
  command that commands the local daemon to bring down the WireGuard interface
  immediately. It requires no cloud credentials, internet access, or Pulumi state,
  ensuring you can restore standard network routing if a remote server disappears.
- **Daemon startup sanitization**: On launch, the daemon inspects the interface
  via `reset_stale_tunnel`. If an interface is up with no handshake recorded or its
  most recent handshake is older than 180 seconds, it is automatically disconnected
  before serving clients, preventing a stale tunnel from capturing default routes.
  Only a tunnel Wisp created is ever touched (see below), so a reboot cannot take
  down someone else's VPN.
- **Tunnel ownership**: `wg0` is the default name of every WireGuard setup and
  `wg-quick` keeps it at the same `/etc/wireguard/wg0.conf` path, so ownership is
  decided by a marker line (`# Managed by wisp`), not by the path. The daemon
  refuses `connect`/`disconnect` on a config without the marker, skips it at
  startup, reports no handshake age for it, and `status` exposes the conflict
  (`Response.tunnel_conflict`) so the CLI and TUI refuse **before** provisioning.
  A conflict costs no billable VM, and there is deliberately no coexistence mode:
  two full-tunnel VPNs fight over the default route.
- **Windows demand start**: On Windows, tunnel services are installed with
  `sc.exe config ... start= demand`, preventing Windows from resurrecting a dead
  tunnel upon reboot.

## Firewall exposure

The security group (AWS), security list (OCI), or VPC firewall rule (GCP)
opens:

- UDP up to the WireGuard port (the tunnel).
- TCP 22 (SSH, for the remote server configuration).
- All egress.

For the **Custom VPS** provider, nothing is configured on the cloud side:
you must open the WireGuard port as an UDP ingress rule in your provider's
firewall/security list yourself — the server-side installer only configures
the instance's own firewall.

Ingress source is controlled by `force_current_ip`:

- **`False` (default)**: `0.0.0.0/0` — reachable from anywhere.
- **`True`**: `<your current public IP>/32` — reachable only from your current
  IP. This tightens both SSH and the tunnel exposure, at the cost of breaking if
  your public IP changes.

Consider enabling `force_current_ip` for a tighter posture when your IP is
stable.

## Trust boundaries summary

| Boundary | Mechanism | Notes |
| ---------- | ----------- | ------- |
| User ↔ daemon | Unix socket `0660 root:wisp` (Linux) or Named Pipe with SIDs (Windows) | Group-gated privileged control |
| Local ↔ VM | SSH with generated RSA 4096 key | Key stored `0600`/icacls, removed on destroy |
| Local ↔ user-owned VPS | SSH with the user's own key | Key path from `[vps]`/flags; used as-is, never stored by Wisp |
| Internet ↔ VM | Cloud security group/list | Optionally pinned to your `/32` |
| Cloud API | Your cloud credentials/environment | Wisp does not manage credentials; each provider uses its standard CLI/SDK credential chain |

## Operational cautions

- Destroying via `wisp destroy` removes the cloud resources and local secrets;
  the local key is not recoverable afterwards.
- The default `0.0.0.0/0` ingress means a deployed-but-forgotten VM is publicly
  reachable. Destroy stacks you no longer use (`wisp gc --destroy` collects
  leftovers).
- VPS deploys add a WireGuard interface (`wisp-<hex>`) and firewall rules to
  YOUR server; `wisp destroy vps` removes exactly that interface's config and
  rules — nothing else on the server.
