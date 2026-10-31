# `wisp.wireguard`

Orchestrates the remote WireGuard server (via Paramiko SSH/SFTP) and the local
client (via the privileged daemon).

## `wireguard/remote_server.py`

### `_connect_ssh(ssh_user, instance_ip, ssh_key_file) -> paramiko.SSHClient`

Shared SSH connection helper. Tries to load the private key as Ed25519, ECDSA,
then RSA (OpenSSH or PKCS#8); when `ssh_key_file` is empty, connects without an
explicit key so Paramiko falls back to the SSH agent and default key
locations (the VPS flow). The client is closed on a failed connect.

### `configure_remote_server(inventory_context: InventoryContext) -> None`

Configures the remote WireGuard server using Paramiko (SSH/SFTP):

1. Establishes an SSH connection via `_connect_ssh`.
2. Uploads the packaged `wireguard-server-install.sh` (package data at
   `src/wisp/data/`) via SFTP to
   `/tmp/wireguard-install.sh` and makes it executable.
3. Executes the script remotely with `sudo` (via a pseudo-terminal), passing
   the configuration as environment variables: `SERVER_PUB_IP`, `SERVER_PORT`,
   `ALLOWED_IPS`, `SERVER_WG_NIC`, `SERVER_WG_IPV4`, `SERVER_WG_IPV6`,
   `CLIENT_DNS_1`, `CLIENT_DNS_2`, `AUTO_INSTALL=y`, `CLIENT_NAME`,
   `CLIENT_WG_IPV4`, `CLIENT_WG_IPV6`, `SKIP_CLIENT_CREATION`.
4. Before installing, probes which package manager the server has and which of
   `wg`/`qrencode` are missing. After a successful install it writes those
   package names, one per line, to `/etc/wisp/installed-packages` (mode
   `0600`). The probe failing only disables cleanup — it never fails a deploy.
4. Locates the generated client config (`wg0-client-*.conf`, with a fallback to
   the SSH user's home directory) and downloads it via SFTP to
   `WISP_DATA_DIR/wireguard-confs/wg0-client.conf` (creating the parent
   directory first).
5. Ensures `PersistentKeepalive = 25` is in the client configuration
   (`ensure_persistent_keepalive`).
6. Arms the autonomous in-VM session watchdog timer (`install_session_watchdog`).

See [deployment flow](../deployment-flow.md#deploy).

### `ensure_persistent_keepalive(conf_path: Path) -> bool`

Ensures `PersistentKeepalive = 25` is present in the WireGuard client configuration
file under the `[Peer]` section. Because the in-VM watchdog checks handshake recency
to detect session abandonment, keepalive packets are required so that an
idle-but-connected tunnel continues generating handshakes and is not falsely
terminated. Returns `True` if added, `False` if already present or on error.

### `build_watchdog_config(nic: str, mode: str, ttl_minutes: int, idle_seconds: int, server_started_epoch: int) -> str`

Generates the `/etc/wisp/watchdog-<nic>.conf` shell configuration for the in-VM
watchdog script. Computes `DEADLINE_EPOCH` from `server_started_epoch + ttl_minutes * 60`
(or `0` when unlimited).

### `install_session_watchdog(ssh, *, nic, mode, ttl_minutes, idle_seconds, ssh_user) -> bool`

Installs and enables the in-VM session watchdog systemd timer on the remote server:

- Reads remote server time via `date +%s` to compute deadlines against the server's
  own clock, eliminating client/server clock skew.
- Uploads the packaged `wisp-watchdog.sh` (`WATCHDOG_SCRIPT_PATH`) via SFTP to
  `/usr/local/bin/wisp-watchdog` (mode `0755`).
- Writes `/etc/wisp/watchdog-<nic>.conf` (mode `0600`).
- Writes templated systemd units `/etc/systemd/system/wisp-watchdog@.service` and
  `wisp-watchdog@.timer` (configured to run every 60 seconds).
- Runs `systemctl daemon-reload` and enables/starts `wisp-watchdog@<nic>.timer`.

### `remove_wisp_server_configs(ssh_user, instance_ip, ssh_key_file="", on_progress=None) -> bool`

Tears down every Wisp-created WireGuard interface on a remote server
(used by the VPS provider's `delete_vm`).
1. Disarms the watchdog for each interface: disables `wisp-watchdog@<nic>.timer`
   and removes `/etc/wisp/watchdog-<nic>.conf`.
2. For each `/etc/wireguard/wisp-*.conf`: `wg-quick down <nic>` (the config's
   `PostDown` rules remove the iptables/firewall rules the installer added),
   `systemctl disable --now wg-quick@<nic>`, and deletes the config file and
   client configs.
3. If `/etc/wireguard/params` is owned by the Wisp interface, it is removed.
4. Checks if any other `/etc/wisp/watchdog-*.conf` remains. If no other Wisp
   sessions exist on the server, removes `/etc/systemd/system/wisp-watchdog@.*`,
   `/usr/local/bin/wisp-watchdog`, reloads systemd, and removes `/etc/wisp`.
Only `wisp-*` files are matched — a pre-existing `wg0.conf` or any other
config is never touched, and packages are kept here. Best effort per
interface: a failed step for one config does not stop the rest. Returns
`False` when the SSH connection or the listing fails.

### `remove_installed_packages(ssh_user, instance_ip, ssh_key_file="", on_progress=None) -> bool`

Removes the WireGuard packages Wisp added to a server it does not own (used
by the VPS provider's `delete_vm`, after `remove_wisp_server_configs`).

1. Lists `/etc/wireguard/*.conf` excluding `wisp-*.conf`. If anything else is
   there, the server has its own tunnel: packages are left alone.
2. Reads `/etc/wisp/installed-packages` — the provenance manifest written by
   `configure_remote_server` with only the packages that were missing before
   the install. No manifest means Wisp never installed anything, so nothing is
   removed.
3. Keeps only names in `REMOVABLE_PACKAGES` (`wireguard`, `wireguard-tools`,
   `qrencode`, `libqrencode-tools`). `iptables` and `resolvconf` are never
   removed even when Wisp installed them, because the server may depend on
   them for its own firewall or DNS.
4. Detects the package manager on the server (`apt-get`, `dnf`, `yum`,
   `pacman`, `apk`) and removes the packages with the matching command. The
   manifest is deleted only after a successful removal, so a failure stays
   retryable.

Returns `True` when the step completed (including deliberate skips) and
`False` when the SSH connection, the manifest read, or the removal failed.

The in-VM watchdog does not call this: its script only estimates handshake age
and tears the tunnel down, deliberately without running a package manager. A
session an abandoned VPS watchdog ended therefore leaves the packages and the
manifest behind, and a later `wisp deploy` on that server reuses them.

## `wireguard/local_client.py`

Thin client that talks to the privileged daemon over the platform transport
(Unix socket on Linux, Named Pipe on Windows — selected by
`daemon.transport.get_transport()`).

- `connect_wireguard_client(config_content: str) -> Response` — `CONNECT` with
  the config body.
- `disconnect_wireguard_client() -> Response` — `DISCONNECT`.
- `status_wireguard_client() -> Response` — `STATUS`.

Each function sends one `Request` and returns its `Response`; the transport
raises a descriptive `PermissionError` if the socket cannot be connected
(typically missing `wisp` group membership).

## `InventoryContext` (`wisp.schemas`)

`TypedDict` of the variables consumed by `configure_remote_server`:
SSH user, instance IP, SSH key file, WireGuard port, allowed IPs, public IP,
interface, IPv4/IPv6, DNS 1/2, client name, client IPv4/IPv6, `skip_client`,
`watchdog_mode` (`"cloud"` or `"vps"`), `watchdog_ttl_minutes`, and
`watchdog_idle_seconds`. Populated during deploy from provider outputs and the
active `WispConfig`.
