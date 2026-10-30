# Troubleshooting

Field-tested fixes — most of these were hit, diagnosed, and resolved in real
sessions. When something fails, `--log-level debug` on any CLI command (or
`WISP_LOG_DIR/wisp.log`, e.g. `~/.local/state/wisp/log/wisp.log` on Linux)
usually names the exact step. Daemon-side issues are diagnosed with
`journalctl -u wisp.service` on Linux; on Windows the daemon logs to
`C:\ProgramData\wisp\logs\wisp.log`.

## Daemon and socket

### `PermissionError` when the CLI/TUI talks to the daemon

The Unix socket is group-restricted (`0660 root:wisp`). You must be a member
of the `wisp` group **and** the membership must be active in your login
session:

```bash
groups | grep wisp        # member?
sudo usermod -aG wisp $USER
newgrp wisp               # or log out and back in
```

### Daemon not running / not responding

```bash
systemctl status wisp.service       # is it active?
journalctl -u wisp.service -n 40    # why did it fail?
sudo systemctl restart wisp.socket wisp.service
```

On Windows: `Get-Service WispService` and the Windows Event Viewer.

### `sudo wisp daemon install` says `command not found`

`sudo` does not inherit your `PATH`; it resets it to its own `secure_path`
(typically `/usr/sbin:/usr/bin:/sbin:/bin`). `uv tool install` puts the `wisp`
console script in `~/.local/bin`, which is **not** in that list, so `wisp`
works as your user and disappears the moment you prefix it with `sudo`:

```console
$ wisp daemon install
wisp daemon install must run as root (try: sudo env PYTHONDONTWRITEBYTECODE=1 "/home/you/.local/bin/wisp" daemon install).
$ sudo wisp daemon install
sudo: wisp: command not found
```

Use the absolute path of the script instead. Wisp prints exactly the line you
need, so copy it from the error message:

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 "$(command -v wisp)" daemon install
```

Both parts matter:

- `$(command -v wisp)` resolves the path in *your* shell, where `PATH` is
  intact, and hands `sudo` an absolute path it cannot lose.
- `PYTHONDONTWRITEBYTECODE=1` stops the root-run process from writing
  root-owned `__pycache__` directories into your user-owned tool venv. Those
  leftovers break the next `uv tool install --force` with a permission error.

The same applies to `wisp daemon uninstall`. `scripts/install.sh` already uses
this exact form, which is why the installer works while a manual
`sudo wisp ...` does not.

Adding `~/.local/bin` to sudo's `secure_path` works too, but it is a
system-wide policy change and Wisp does not require it.

### The daemon rejects the client config

The daemon rejects client configs containing `PreUp`/`PostUp`/`PreDown`/
`PostDown` — `wg-quick` would execute them as root, so the socket boundary
does not allow them. This is intentional; see [security.md](security.md).

## Deploys

### Pulumi fails with auth/lock errors

- Deployments need an authenticated Pulumi backend: `pulumi login` (the state
  lives on Pulumi Cloud by default). If you use local state with a passphrase,
  make sure `PULUMI_CONFIG_PASSPHRASE` is set.
- A crashed deploy can leave the stack locked: `wisp gc` shows it, and
  `pulumi cancel -s <stack>` clears the lock.

### The tunnel comes up but there is no internet

Work the layers in order — each one was a real failure during development:

1. **Handshake first** (client): `sudo wg show wg0` — if `latest handshake` is
   old or missing and `transfer: received` is 0 B, the UDP port is blocked.
   The cloud firewall/security list must allow **UDP on the WireGuard port**,
   and the instance firewall must let it through (on Oracle Linux OCI images,
   firewalld's default zone only allows SSH — open the port with
   `firewall-cmd --add-port=<port>/udp`). `sudo tcpdump -ni any 'udp port
   <port>'` on the server tells you whether the packets even arrive.
2. **Server forwarding**: `sudo sysctl net.ipv4.ip_forward` must be `1` (the
   installer sets it via `/etc/sysctl.d/wg.conf`).
3. **NAT**: the installer adds `MASQUERADE` rules for the server's default
   NIC — verify with `sudo iptables -t nat -S | grep MASQ`.

### Oracle Linux specifics

- The installer creates `/etc/wireguard/` with mode `0700` and files `0600` —
  an earlier `chmod 600 -R` (no execute on the directory) made `wg-quick`
  fail with "conf does not exist" even though the file was there.
- Re-running the installer with a stale `/etc/wireguard/params` triggers its
  interactive manage menu — Wisp's deploy pre-cleans that state for you.
- OL8 needs `oraclelinux-developer-release-el8` for `wireguard-tools`; OL9+
  ships it in the base repos.
- With firewalld running, `firewall-cmd` inside wg-quick's `PostUp` fails
  from a systemd unit (dbus denies the sender on EL9) — Wisp configures the
  zone at install time instead.

### "Deleting" a VPS session does not delete a VM

Correct — a Custom VPS is your server. The destroy tears the tunnel down on
both ends and removes Wisp's own configs; the machine, packages, and any
non-Wisp WireGuard config stay untouched.

## "The TUI shows an orphaned session but the tunnel is clearly up"

The TUI only adopts a live tunnel when the daemon reports a WireGuard
handshake, and an **outdated daemon** cannot: it answers `status` without the
`handshake_age_seconds` field the client needs. The CLI and the daemon are
separate installations, so a refreshed CLI is not a refreshed daemon.

An outdated daemon no longer produces a destructive offer: an interface that is
up but reports no handshake age shows a **"Session state unknown"** notice
instead of "clean up the orphan", because there is no evidence the tunnel is
dead. Treat that notice as a daemon-version warning first.

Check what the daemon actually returns:

```bash
uv run python -c "import json,socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/wisp.sock'); s.sendall(b'{\"action\":\"status\"}\n'); print(json.loads(s.makefile().readline()).keys())"
```

If `handshake_age_seconds` is missing, reinstall the daemon — it rebuilds the
venv and restarts the service itself:

```bash
sudo wisp daemon install
```

If `sudo` answers `wisp: command not found`, its `secure_path` does not include
`~/.local/bin`. Call the console script by absolute path (this is what
`scripts/install.sh` does):

```bash
sudo env PYTHONDONTWRITEBYTECODE=1 "$(command -v wisp)" daemon install
```

An editable CLI makes the daemon mirror your checkout, so this is also the
command to run after changing daemon-side code.

## Leftovers and orphans

- On startup, Wisp inspects any session left behind by a dead process. If that
  session still has a **live** local interface with a fresh WireGuard
  handshake, the TUI adopts it: it opens the tunnel view, and quitting again
  destroys the tunnel. The marker (start and expiry) is left untouched, so
  adoption never extends a session — and it is not reported as an orphan,
  because it is not one.
- Only when that tunnel is gone or stale does Wisp warn about a leftover
  session and offer to clean it up; its cloud resources may still be billing.
- `wisp gc [--refresh] [--destroy]` inspects leftover Pulumi stacks directly
  from their state. With a local/file Pulumi backend, losing the machine
  loses the state — orphans become invisible until you find them in your
  provider console.
- After `gc --destroy`, the local SSH key and client conf of the destroyed
  sessions are removed too.

## "Another Wisp operation is in progress"

Pulumi locks a stack while an update is running, so a deploy or destroy that
collides with another one fails instead of corrupting state. Wisp surfaces that
as a plain message naming the cause. It is not an error in your setup: a
scheduled `wisp destroy` (the TTL watchdog) is the usual reason.

- Wait for the running operation to finish and retry.
- If the TUI shows "Destroying resources", another process is tearing the
  session down right now; it returns to the menu automatically when it finishes.
- If Pulumi reports a stale lock from a crashed run, follow Pulumi's own
  recovery guidance for that backend (`pulumi cancel` / the stack's lock file).
  Wisp does not keep a lock of its own.

## Client-side oddities

- **No internet after a reboot or suspend**: If your workstation rebooted, went to
  sleep, or lost power while a tunnel was up, the local interface may still hold
  the default route (`0.0.0.0/0`) directed to a server that is unreachable.
  - Run the panic disconnect command: `wisp disconnect`. This immediately drops
    the local tunnel interface via the daemon without needing cloud API calls.
  - Built-in fail-safes prevent recurring issues: on Windows, Wisp configures the
    tunnel service with `start= demand` (`sc.exe config WireGuardTunnel$wg0 start= demand`)
    so a reboot cannot bring the interface back up. Furthermore, the Wisp daemon
    runs `reset_stale_tunnel` on startup, automatically tearing down any local
    tunnel that has never completed a handshake or whose latest handshake is
    older than 180 seconds.
- **Session destroyed or VM powered off on its own**: Ephemeral VMs self-terminate
  via the in-VM watchdog (`/usr/local/bin/wisp-watchdog`) when either of two
  conditions is met:
  1. **Hard TTL expired**: The configured TTL (`[general].max_session_ttl_minutes`
     or CLI `--ttl`) passed its server-clock deadline.
  2. **Idle timeout**: No WireGuard handshake occurred for 15 minutes (900 seconds),
     or 15 minutes elapsed since deployment without an initial handshake.
  To prevent false idle triggers during quiet browsing, Wisp automatically appends
  `PersistentKeepalive = 25` to the client configuration. If you closed your laptop
  or disconnected for over 15 minutes, the remote server autonomously shuts down
  (`systemctl poweroff` on AWS/GCP/OCI) or tears down the Wisp interface (Custom VPS).
  For long unattended sessions, set `--ttl 0`.
  - If this happens while the TUI is open, the tunnel view notices that no
    handshake has arrived for several minutes and switches to a "Session ended"
    screen instead of showing a tunnel that no longer exists. From there you can
    reconcile the leftover state (the destroy refreshes first, so it is safe even
    when the resources are already gone) or return to the menu.
  - A local interface that is merely **down** (`wisp disconnect`) does not trigger
    that screen: a downed local interface says nothing about the remote end.
- **Windows: script fails right after edits** — check for CRLF line endings
  if the `.sh`/`.conf` files were edited on Windows (`dos2unix`).
- **PowerShell quoting**: environment variables with `$` in the `wisp.toml`
  example values need single quotes.
