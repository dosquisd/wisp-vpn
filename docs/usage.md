# Usage

Wisp has two modes: an interactive TUI (default) and non-interactive CLI
subcommands. Both are dispatched from `wisp.main:main` (`parse_args` + `main`).

## Launching

```bash
wisp            # no subcommand → launches the TUI
wisp deploy     # CLI: deploy a VM
wisp destroy    # CLI: destroy VMs
wisp regions    # CLI: list available regions
wisp daemon     # CLI: install/uninstall the privileged local daemon
```

## CLI subcommands

Every provider-facing subcommand accepts a positional `provider` argument
(default `aws`; `aws`, `oci`, `gcp`, and `vps` today — see `ProviderEnum` for
the current list). `gc` and `daemon` are provider-independent.

> **Note:** Wisp ships with support for `aws`, `oci`, `gcp`, and `vps`
> (bring-your-own-server) as of this writing, and the provider layer is
> extensible. For the current list, see `ProviderEnum`
> ([`src/wisp/providers/base.py`](../src/wisp/providers/base.py)) and
> `PROVIDERS_MAP`
> ([`src/wisp/providers/__init__.py`](../src/wisp/providers/__init__.py)).

### `wisp deploy [provider] [-r/--region REGION] [--log-level LEVEL]`

Provisions a VM and brings the tunnel up. For `vps`, skips provisioning
entirely and configures WireGuard on the server you point it at.

- `provider` — cloud provider (`aws`, `oci`, `gcp`, or `vps`, default `aws`).
- `-r`, `--region` — target region. If omitted, the region is resolved from
  the provider configuration in `wisp.toml` (`[aws].region`, `[oci].region`,
  or `[gcp].region`). There is no cross-provider default region. Ignored for
  `vps`.
- `--ttl MINUTES` — session TTL watchdog: schedules an OS-level timer that
  runs `wisp destroy` after this many minutes even if Wisp exits (default:
  `[general].max_session_ttl_minutes`; `0` disables it).
- `--wireguard-port PORT` — UDP port for the tunnel (default:
  `[general].wireguard_port`; `0` picks a random port).
- VPS only: `--host HOST`, `--ssh-user USER`, `--ssh-port PORT`,
  `--ssh-key-path PATH` — connection overrides for this run (the TOML
  `[vps]` section is the fallback).
- `--log-level LEVEL` — console verbosity (`debug`/`info`/`warning`/`error`,
  default `info`). The rotating file log always keeps `DEBUG`.

**Examples:**

```bash
wisp deploy aws --region eu-west-1
wisp deploy oci            # uses the default region from [oci] in wisp.toml
wisp deploy aws -r sa-north-1 --ttl 120

# Bring-your-own-server (no cloud credentials needed):
wisp deploy vps --host 203.0.113.10 --ssh-user root --ssh-key-path ~/.ssh/id_ed25519
```

Prints the deployment result as JSON, e.g.:

```json
{
  "instance_id": "i-0123456789abcdef0",
  "public_ip": "203.0.113.10",
  "private_ip": "10.0.0.5",
  "wireguard_port": 51820
}
```

### `wisp destroy [provider] [-r/--region REGION] [--log-level LEVEL]`

Disconnects the local client, destroys the Pulumi stack (cancelling any
in-flight update first), and removes local artifacts (SSH key, client
config). For `vps`, it also tears the tunnel down on the server: every
`/etc/wireguard/wisp-*.conf` interface is brought down and deleted
(packages and non-wisp configs are never touched). Prints the number of
deleted resources:

VPS connection details resolve in this order: `--host`/`--ssh-user`/
`--ssh-port`/`--ssh-key-path` flags, then the deploy-time marker (what the
deploy actually used — the TUI writes it automatically), then the `[vps]`
TOML section.

```bash
wisp destroy aws -r eu-west-1
# → Deleted VM. Count: 4

wisp destroy oci -r sa-bogota-1
# → Deleted VM. Count: 2
```

### `wisp regions [provider]`

Lists the regions available for the provider (AWS via `boto3` `describe_regions`,
OCI via the Identity API, GCP via the Compute SDK; `vps` returns a
placeholder). Prints a JSON array.

```bash
wisp regions aws
wisp regions oci
```

### `wisp gc [--refresh] [--destroy]`

Garbage collection for leftover Pulumi stacks: lists every `wisp-stack-*`
in the backend, counts the real resources recorded in each stack's exported
state (no program run — safe even with broken credentials), and reports the
leftovers.

- `--refresh` — reconciles each stack's state against the cloud first, so
  resources you deleted out-of-band (e.g. a VM removed by hand in the
  console) stop counting as leftovers.
- `--destroy` — destroys every stack that still holds resources (with a
  best-effort refresh first). Requires working provider credentials.

```bash
wisp gc                 # report only
wisp gc --refresh       # reconcile state with reality, then report
wisp gc --destroy       # tear down every leftover stack
```

### `wisp daemon install | uninstall`

Installs or removes the privileged local daemon that the frontend talks to
(`scripts/install.sh`/`setup.ps1` call it as part of a full install; this
subcommand manages the daemon on its own). Requires root/sudo on Linux and an
elevated terminal on Windows.

- **Linux** (systemd, socket-activated): builds a root-owned venv at
  `/usr/local/lib/wisp/venv` with `wisp-vpn` installed into it (validated
  before anything systemd-related runs — a root service must not execute
  user-mutable code, which is why the invoking user's `uv tool` venv is never
  used), writes `/etc/systemd/system/wisp.socket` and `wisp.service` (rendered
  from package data with that venv's interpreter), creates the `wisp` group
  that owns `/run/wisp.sock`, enables and starts the socket, and — when run
  under `sudo` — adds your user to the `wisp` group. **Log out and back in**
  (or `newgrp wisp`) so the group membership takes effect. Re-running
  `install` refreshes the units: it stops and disables any existing units
  first.
- **Windows**: installs the native service via the pywin32 wrapper and sets it
  to start automatically (`uninstall` removes it).

`uninstall` disables and deletes the two systemd units and removes the
root-owned daemon runtime at `/usr/local/lib/wisp` (Linux), then runs
`systemctl daemon-reload`/`reset-failed`. The `wisp` **group is kept** — its
lifecycle (remove vs keep) belongs to the `scripts/uninstall.sh` bootstrap,
which records in the install manifest whether the group was created by Wisp.

```bash
sudo wisp daemon install
sudo wisp daemon uninstall
```

## CLI vs TUI behaviour

- **TUI mode** (no subcommand): `WispApp().run()` is started and the process
  exits when the TUI closes. Deploy progress is shown live via the
  `on_progress` callback and a progress bar.
- **CLI mode**: the console log handler is raised to `DEBUG` so you see detailed
  logs on stderr, and the boot wait is shown with a `tqdm` progress bar. Results
  are printed as JSON / plain text and the process exits with code `0`.

Argument parsing normalizes `command`, `provider`, and `region` to lowercase. If
no command is given, it defaults to the TUI.

## The TUI

The TUI is built with [Textual](https://textual.textualize.io/). Global styles
and screen registration live in `cli/app.py`; per-session state lives in
`cli/state.py` (`AppState`).

### Screens

| Screen | File | Purpose |
| ------ | ---- | ------ |
| Main menu | `screens/main_menu.py` | Entry point; shows current config summary; navigate to Deploy/Config/Quit |
| Configuration | `screens/config.py` | Edit per-session `WispConfig` (in memory) |
| Deploy | `screens/deploy.py` | Pick provider + region, review summary, start deploy |
| Progress | `screens/progress.py` | Live deploy/destroy progress; shows results; destroy button |

### Navigation and key bindings

- Main menu: `1` deploy, `2` config, `3` quit. `q` / `Ctrl+C` quit anywhere.
- All screens: the arrow keys navigate between widgets — `Up`/`Down` (or
  `Tab`) move forward/backward in DOM order, and `Left`/`Right` do the same,
  which is what most people try inside a horizontal button group. The
  confirmation modals inherit the same arrows, so `Up`/`Down` (and
  `Left`/`Right`) move between their buttons too. While an `Input` or `Select`
  is focused, its own arrow handling wins (text cursor / option list), so
  typing and picking are never hijacked.
- Config / Deploy / Progress: `Escape` goes back.

### Configuration screen

Edits an in-memory `WispConfig` for the session (persisted to the active
`wisp.toml` only when you press *Save*). Fields:

- `vm_boot_timeout` (seconds, minimum 5)
- `wireguard_port` (`0` = random in `49152–65535`, otherwise `0–65535`)
- `wireguard_interface` (non-empty)
- Primary / secondary DNS (both non-empty)
- `max_session_ttl_minutes` (default TTL for the OS-level watchdog; `0` = off)
- Restrict access to your current public IP only (toggle → `/32` firewall rule)

Validation errors are shown inline. Both *Save* and *Reset* ask for
confirmation first: saving opens a "Save settings?" modal (the form is
validated before the modal appears), and resetting confirms before the form
is reloaded from `wisp.toml`.

Unsaved edits are tracked: a yellow "Unsaved changes" marker appears as soon
as a field differs from what was loaded. Leaving via `Escape`/*Back* or
pressing *Reset* while there are unsaved edits opens the discard modal — you
can *Save* (persist and leave), *Discard* (revert), or *Cancel* (stay and
keep editing). Cancelling any confirmation modal never loses edits: the dirty
marker stays. The provider credential collapsibles are read-only; the "Show
full values" toggle only reveals them.

### Deploy screen

- Provider select (`aws`, `oci`, `gcp`, or `vps`).
- Region select. On mount, it fetches live regions in a background thread;
  if the fetch fails (e.g. expired credentials), the Select is repopulated
  with the provider's standard list instead of keeping another provider's
  regions. Hidden for `vps`.
- VPS connection fields (shown only for `vps`): host, SSH user, SSH port,
  SSH private key path, and WireGuard port (`0` = random). The form is the
  source of truth for the deployment; the TOML `[vps]` section only pre-fills
  it.
- Session TTL watchdog row (switch + minutes) — arms the OS-level timer for
  this deployment.
- A summary reflects the current config; "Start Deployment" pushes the
  Progress screen which runs the deployment in a worker thread.

### Progress screen

- Runs `deploy_vm` / `delete_vm` in a worker thread and marshals progress back
  to the UI thread via `call_from_thread`.
- The session guard is armed before provisioning starts: quitting mid-deploy
  cancels the in-flight update and destroys partial resources.
- The progress bar is centred above the status line. During the WireGuard
  install the remote installer's own output is streamed live into the status
  line (throttled to one line per second), so a minutes-long install never
  looks like a hang.
- On success, shows instance ID, public IP, WireGuard port, and private IP,
  plus a "Destroy VPN" button that runs the destroy flow.
- On failure, shows a scrollable error box with the real Pulumi diagnostics
  (extracted from the command result, not the bare "code: 1") and both
  "Destroy Resources" and "Back to Menu" buttons — partial resources are
  billable, so the cleanup path is always available.

### Tunnel screen

The tunnel view is a terminal state: `q`, `Ctrl+C`, `esc`, `d`, and the
*Destroy Tunnel and Exit* button all tear the VPN down and return to the main
menu — there is deliberately no way back to the menu with a live session (a
tunnel nobody watches is a VM nobody tears down). Because that destroy is
irreversible, a *Confirmation* modal asks first unless
`[general].confirm_destroy` is `false` — `q` and `Ctrl+C` ask too when a
tunnel is live, while on every other screen they quit straight away. The
dialog's "Don't ask again" checkbox turns that off and persists it.
