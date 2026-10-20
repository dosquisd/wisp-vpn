# Deployment flow

This document walks through what happens on deploy and destroy, mapping each
step to the code that performs it.

## Deploy

Entry: `Provider.deploy_vm(region, force_current_ip=False, config=None, on_progress=None)`
(`providers/pulumi_base.py`, shared by all Pulumi-based providers — `aws`,
`oci`, and `gcp` today; see `ProviderEnum` for the current list). The `vps`
provider (`providers/vps.py`) skips steps 2–4 entirely: it SSHes straight into
the user's server and runs the same installer (step 6), using a
`wisp-<hex>` server-side interface name instead of `wg0`.

**Before provisioning starts**, the Progress screen writes the active-session
marker and arms the `SessionGuard`: a TUI or console death mid-deploy leaves
the marker behind and lets the signal handlers cancel the in-flight update
and destroy partial resources. If `[general].max_session_ttl_minutes > 0`,
arming the guard also schedules the OS-level TTL watchdog
(`utils/watchdog.py`). The guard is re-armed unchanged on success and on
completed failure.

1. **Resolve config.** If `config` is `None`, a `WispConfig` is built from the
   `[general]` TOML section via `load_wisp_config()` (and the `force_current_ip`
   argument, when true, overrides it); otherwise `force_current_ip` is taken
   from the provided config.

2. **Provision with Pulumi.** `create_or_select_pulumi_stack(program, provider=...)`
   selects (or creates) the provider-specific stack (`wisp-stack-aws`,
   `wisp-stack-oci`, or `wisp-stack-gcp`) in project `wisp-project`, then `stack.up()` runs the
   provider-specific program:
   - `ensure_plugins()` installs the provider plugins (`aws` `v7.44.0`,
     `oci` `v4.22.0`, `gcp` `v9.37.0`, `tls` `v5.5.1`) once per process.
   - The provider program declares the infrastructure: for AWS,
     `create_ec2_instance` (security group + EC2 instance); for OCI,
     `create_oci_instance` (VCN, Internet Gateway, Route Table, Security List,
     Subnet + Compute Instance); for GCP, `create_gcp_instance` (VPC firewall
     rule + SPOT Compute Instance). All of them open UDP for the WireGuard
     port and TCP 22 for SSH, generate an RSA 4096 key pair via
     `tls.PrivateKey`, and export the stack outputs.
   - Stack **outputs**: `instance_id`, `instance_public_ip`,
     `instance_private_ip`, `instance_private_key`, `wireguard_port`, `ssh_user`.

3. **Wait for boot.** Sleeps `config.vm_boot_timeout` seconds (default 60),
   reporting progress via `on_progress` (TUI) or `tqdm` (CLI).

4. **Persist the private key.** Reads the `instance_private_key` output and writes
   it to `keys/wireguard-key.pem` with mode `0600` (Linux) or secured via
   `icacls` with well-known SIDs (Windows).

5. **Compute AllowedIPs.** `<your-ip>/32` if `force_current_ip`, else
   `0.0.0.0/0,::/0`.

6. **Configure the remote server.** `configure_remote_server(context)`
   (`wireguard/remote_server.py`) runs:
   - Establishes SSH connection to the VM using the private key (Paramiko,
     supporting Ed25519, ECDSA, RSA key formats).
   - Uploads `scripts/wireguard-server-install.sh` via SFTP to
     `/tmp/wireguard-install.sh` (this script **is not optional** — it is the
     WireGuard server installer, based on `angristan/wireguard-install`).
   - Executes the script remotely with configuration passed as environment variables:
     `SERVER_PUB_IP`, `SERVER_PORT`, `ALLOWED_IPS`, `SERVER_WG_NIC`,
     `SERVER_WG_IPV4`, `SERVER_WG_IPV6`, `CLIENT_DNS_1`, `CLIENT_DNS_2`,
     `AUTO_INSTALL=y`, `CLIENT_NAME`, `CLIENT_WG_IPV4`, `CLIENT_WG_IPV6`,
     `SKIP_CLIENT_CREATION`
   - The script installs WireGuard non-interactively, configures the interface,
     firewall, routing, and generates the client configuration
   - Downloads the generated client config (`wg0-client-*.conf`) via SFTP to
     `wireguard-confs/wg0-client.conf`

7. **Connect the local client.** `connect_wireguard_client(conf_text)`
   (`wireguard/local_client.py`) sends a `CONNECT` request with the config
   content to the daemon over `/run/wisp.sock` (Linux) or
   `\\.\pipe\wisp` (Windows). The daemon (`daemon/server.py`) writes the
   WireGuard config (`0600` / icacls) and runs `wg-quick up wg0` (Linux)
   or the native Windows equivalent via its adapter.

8. **Return.** A `DeployVMResult` with `instance_id`, `public_ip`, `private_ip`,
   and `wireguard_port`.

## Destroy

Entry: `Provider.delete_vm(region, on_progress=None)`.

1. **Disconnect the client.** `disconnect_wireguard_client()` sends a
   `DISCONNECT` request; the daemon runs `wg-quick down wg0` (Linux) or
   the native Windows equivalent.

2. **Cancel any in-flight update, then destroy infrastructure.** Selects the
   stack, calls `stack.cancel()` (a no-op when nothing is in flight — this is
   what lets orphan cleanup tear down a stack whose deploy was interrupted),
   then `stack.destroy()`. Errors are logged and cause an early return of `0`.

3. **Clean up local artifacts** (if present):
   - `keys/wireguard-key.pem`
   - `wireguard-confs/wg0-client.conf`

4. **Clear the active-session marker** (`clear_active_session()`). The marker
   is the single source of truth for "there are resources out there", so once
   `delete_vm` has finished it must go — otherwise the next startup would offer
   to "clean up" an orphan whose cloud resources no longer exist. This happens
   in `teardown_active_session()` after `delete_vm` returns, and in the CLI
   `wisp destroy` when the destroy matches the marker's provider + region. If
   `delete_vm` raises, the marker stays on purpose so the orphan is still
   detectable next run.

5. **Return** the number of deleted resources
   (`destroy_result.summary.resource_changes["delete"]`, defaulting to `0`).

Throughout, `on_progress` reports granular phases — "Disconnecting the local
tunnel", "Destroying provider infrastructure", "Removing local keys and
configuration", "Resources destroyed successfully" — so the shutdown screen
shows real state instead of one frozen message.

**VPS destroy (`VPSProvider.delete_vm`) additionally tears the tunnel down on
the server.** `remove_wisp_server_configs()`
(`wireguard/remote_server.py`) SSHes in and, for every
`/etc/wireguard/wisp-*.conf`: runs `wg-quick down <nic>` (the config's
`PostDown` rules remove the iptables/firewall rules the installer added),
runs `systemctl disable --now wg-quick@<nic>`, and deletes the config file.
Packages are kept — the server may have been using WireGuard before Wisp —
and any non-wisp config (e.g. the user's own `wg0`) is never touched.

## Session lifecycle

A deployed tunnel is billable cloud infrastructure, so releasing it is a
decision of the session rather than a side effect of one exit path. After a
successful deploy, `ProgressScreen` writes a marker file
(`active-session.json` in the user config directory) and arms a
`SessionGuard` (`session.py`) with a teardown that runs `delete_vm`.

Every trigger funnels into that same guard, and the first one to fire wins:

| Trigger | Mechanism |
| --- | --- |
| `q` / `Ctrl+C` on any screen | `WispApp.action_quit` -> `ShutdownScreen` |
| `esc` / `d` / "Destroy Tunnel and Exit" | `TunnelScreen.action_destroy` -> `ShutdownScreen`. Asks first via a confirmation modal unless `[general].confirm_destroy` is `false`; the modal's "Don't ask again" checkbox persists that setting. |
| `SIGHUP` | Terminal window closed -> `install_signal_handlers` -> guard |
| `SIGTERM` | `kill`, supervisor or session manager -> guard |
| `SIGINT` | Explicit `kill -INT` or shell job control -> guard |
| Interpreter exit | `atexit` hook (all platforms) |
| Previous run died | `find_orphaned_session` warns on next startup |
| TTL expired | OS-level watchdog (`utils/watchdog.py`) runs `wisp destroy <provider> <region>` — this one works even if Wisp is gone |

`ShutdownScreen` owns the actual teardown so the blocking Pulumi destroy shows
progress instead of freezing the interface. `SessionGuard.shutdown()` is
idempotent and lock-guarded, so overlapping triggers cannot destroy twice.

A signal handler replaces the signal's default action, so once the handler
returns the process would keep running with a destroyed tunnel. The handler
therefore restores the terminal and calls `os._exit(128 + signum)` after the
teardown, instead of returning into a dead event loop.

### The tunnel view is a terminal state

Once a deployment succeeds, the tunnel view replaces the deploy screens and
there is **no way back to the menu with a live session**: the VPN is ephemeral,
so every exit destroys it. `q`, `Ctrl+C`, `esc`, `d`, the button and closing the
terminal all release the resources, and the main menu is only ever reachable
while nothing is running.

Esc during an in-flight deploy is the one exception, and it is refused on
purpose: popping the screen while `deploy_vm` runs in a worker would let the
resources come up with no guard armed and no marker written, which is precisely
the invisible orphan this design exists to prevent. The screen explains this and
points at `q` / `Ctrl+C`.

**Why `Ctrl+C` is a key event, not a signal.** Textual's driver clears the
terminal's `ISIG` flag, so `Ctrl+C` arrives as a keypress and hits the
`ctrl+c` binding. That path runs through the same UI as `q`, with progress
visible, instead of interrupting the event loop from a signal context.

**Platform differences.** Signal handlers are POSIX-only: Windows has no
`SIGTERM` or `SIGHUP` delivery to arbitrary processes, and console control
events are shared by the process group. On Windows the teardown is driven by
keybindings and `atexit`. `SIGKILL` cannot be caught on any platform by design;
the marker file is the mitigation, since it outlives a hard kill and lets the
next run report the orphaned resources.

## The TTL watchdog

The marker-based mitigations above only trigger on the *next* launch — if
nobody reopens Wisp, a forgotten VM keeps billing. When
`[general].max_session_ttl_minutes > 0` (default `60`), deploying schedules an
OS-level one-shot that belongs to the OS, not to Wisp (`systemd-run --user`
transient unit on Linux, `schtasks` on Windows) which runs
`wisp destroy <provider> <region>` when the TTL expires. Manual teardown
cancels it (best effort); if Wisp dies, the timer fires alone. Scheduling is
idempotent (cancel-first), so re-arming the session guard reschedules the
same target cleanly. The deploy screen can toggle it per deployment and the
CLI overrides it with `--ttl`; `0` keeps it off.

## Notes on the remote installer

`scripts/wireguard-server-install.sh` is a non-interactive WireGuard server
installer (based on the widely used `angristan/wireguard-install`, MIT). It is
driven entirely by environment variables set by the SSH/SFTP orchestration, so
no interactive prompts are needed. `scripts/wireguard-client-install.sh`
carries its own shell helpers (`isRoot`, `checkOS`, `installPackages`,
`installWireGuardClient`, `uninstallWg`) for the client-side install.

## Operating system differences in the deployment flow

- **Linux**: The daemon listens on `/run/wisp.sock`. The client connects
  via Unix domain socket. The server script is executed with `sudo`.
- **Windows**: The daemon listens on the Named Pipe `\\.\pipe\wisp`. The client
  connects via `WindowsNamedPipeTransport`. The server script is still uploaded
  via SFTP and executed, but paths and permissions differ (`icacls` with SIDs
  instead of `chmod 600`).
