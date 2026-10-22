# Installation

## Quick install (Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/install.sh | bash
```

The script is self-contained (a single downloaded file, no repo checkout
needed). In order, it:

1. Detects your distro and installs `curl` if missing.
2. Installs `uv` if missing (uv brings its own Python 3.14, so nothing else
   to set up).
3. Installs the CLI with `uv tool install wisp-vpn` (refreshes it with
   `--force` when already installed).
4. Installs the WireGuard tools via the distro package manager.
5. Installs the Pulumi CLI per-user under `~/.pulumi` unless `--skip-pulumi`
   is passed.
6. Delegates the privileged part to `sudo wisp daemon install` — systemd
   units, the `wisp` group, and the re-login hint all come from the CLI
   itself. The daemon runtime lives in a root-owned venv at
   `/usr/local/lib/wisp/venv` (a root service must not execute user-mutable
   code); it is removed again by `sudo wisp daemon uninstall`.
7. Writes an `install-manifest.json` under the Wisp data dir, recording
   which tools were already on the machine (`pre_existing`) and which the
   script installed (`installed_by_wisp`), so the uninstaller knows what to
   ask about.

Preview everything without changing the system:

```bash
curl -fsSL https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/install.sh | bash -s -- --dry-run
```

To uninstall, use the paired script. Run it via `bash <(...)` instead of a
pipe so its confirmation prompts can read your terminal:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/uninstall.sh)          # interactive
bash <(curl -fsSL https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/uninstall.sh) --dry-run
```

It removes the daemon (`sudo wisp daemon uninstall`), the `wisp-vpn` tool,
and the Wisp-owned data directories; tools the installer itself installed
are removed after asking, and anything marked pre-existing in the manifest
is left untouched.

## Quick install (Windows)

Run PowerShell as a regular user. The script is self-contained and does not
require a repository checkout:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
irm https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/install.ps1 | iex
```

The installer installs uv, the `wisp-vpn` CLI, WireGuard, Pulumi (unless
skipped), the `wisp` local group, and the native `WispService` service. It
installs the CLI per-user and elevates only the daemon setup. A local
installation can be run with `.\scripts\install.ps1`; it is safe to run again.

To skip Pulumi or preview the plan:

```powershell
& ([scriptblock]::Create((irm `
    https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/install.ps1))) `
    -SkipPulumi
& ([scriptblock]::Create((irm `
    https://raw.githubusercontent.com/dosquisd/wisp-vpn/HEAD/scripts/install.ps1))) `
    -DryRun
```

To uninstall Wisp completely:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\uninstall.ps1
```

The uninstaller invokes `wisp daemon uninstall`, removes the Wisp CLI, service,
group, machine-wide and user data, and dependencies recorded as installed by
Wisp. Pre-existing dependencies are preserved. Use `-DryRun` to preview the
actions or `-Yes` to skip dependency confirmation prompts. Destroy any live
session before uninstalling: uninstalling the client does not destroy cloud
resources, and leftover resources may continue billing.

## Requirements

- Linux host for the client (macOS support is planned; the daemon uses systemd
  socket activation on Linux, or a native Windows service + Named Pipes on
  Windows; the macOS adapter is not implemented yet — see `MacOSAdapter`
  in [`daemon/adapter.py`](../src/wisp/daemon/adapter.py)).
- **Python 3.12+** (see `.python-version` and `requires-python` in
  `pyproject.toml`) — `uv` installs it for you automatically.
- **WireGuard tools** (`wg`, `wg-quick`) on the client — installed by
  `scripts/install.sh` (Linux) or `scripts/install.ps1` (Windows), or manually with your
  distro's package manager.
- **Pulumi CLI** — installed by `scripts/install.sh` (Linux, unless
  `--skip-pulumi` is passed) or `scripts/install.ps1` (Windows, unless
  `-SkipPulumi` is passed).
- **Cloud credentials** available in the environment (for example via the AWS
  CLI, `~/.aws/credentials`, or `~/.oci/config`), with permissions to manage
  instances, security rules, and key pairs, **or** configured in `wisp.toml`.

## Python dependencies

All installed automatically by `uv tool install` (the CLI) and by `pip` for
the daemon:

- Core: `textual` (TUI), `paramiko` (SSH/SFTP), `tomli-w` (TOML writing),
  `tqdm` (CLI progress).
- Cloud SDKs: `boto3` (AWS), `oci` (Oracle), `google-cloud-compute` (GCP).
- Pulumi: `pulumi`, `pulumi-aws`, `pulumi-oci`, `pulumi-gcp`, `pulumi-tls`.
- Windows only: `pywin32` (Named Pipe transport + Windows service).

## Installing the CLI

Install the package as an editable tool so the `wisp` command is available:

```bash
uv tool install --editable .
```

## Installing the privileged daemon

### Linux

The client-side daemon must run as root so it can bring the local WireGuard
interface up and down. `wisp daemon install` installs it. **Run it as root**
(it checks and exits otherwise):

```bash
sudo wisp daemon install
```

What it does, in order:

1. Verifies it is running as root and that systemd is present.
2. Warns if the WireGuard tools are missing (they can be installed later).
3. Creates the `wisp` group (if missing) — it gates access to the daemon socket.
4. Builds a root-owned venv at `/usr/local/lib/wisp/venv` from the first
   suitable interpreter and installs `wisp-vpn` into it — this is what the
   systemd unit executes, because a root service must not run user-mutable
   code.
5. Writes `wisp.socket` and `wisp.service`, reloads systemd, enables/starts
   the socket, and restarts the service.

> After running it, log out and back in (or run `newgrp wisp`) so your new
> `wisp` group membership takes effect. Until then, connecting to the daemon
> socket fails with a `PermissionError`.

### Windows

The installation is performed with `scripts/install.ps1`, which is
self-contained and installs WireGuard Windows exe, Pulumi via `winget`, and a
native Windows service for the daemon:

1. Installs `uv` if not already present.
2. Installs the `wisp-vpn` CLI from PyPI.
3. Installs Pulumi via `winget` unless `-SkipPulumi` is passed.
4. Installs the WireGuard Windows executable.
5. Creates the `wisp` local group and adds the current user to it.
6. Installs the Wisp Windows service from the uv-managed environment.
7. Starts the Wisp service and records an install manifest.

The daemon supports both systemd socket activation (Linux) and Windows Named
Pipes (`WindowsNamedPipeTransport`) for communication with the CLI/TUI.

## Python dependencies

Declared in `pyproject.toml` and installed automatically by `uv tool install`
(the CLI) and `pip` (the daemon):

- `pulumi` (`>=3,<4`), `pulumi-aws` (`>=7,<8`), `pulumi-oci` (`>=4.22.0`), and
  `pulumi-gcp` (`>=9.37.0`) — infrastructure provisioning.
- `pulumi-tls` — generates the RSA 4096 key pair for SSH.
- `boto3` — AWS SDK (regions, credentials).
- `oci` — OCI SDK (regions, credentials).
- `google-cloud-compute` — GCP SDK (regions, credentials).
- `paramiko` — SSH/SFTP remote server configuration.
- `platformdirs` — per-user data/config/log directory resolution.
- `textual` — the terminal UI.
- `tomli-w` — writes `wisp.toml` when the TUI saves settings.
- `tqdm` — CLI-mode progress bar.
- `pywin32` (Windows only) — Named Pipe transport + Windows service.

## Runtime directories

These are created/populated at runtime, independent of where you launch Wisp:

| Path | Contents |
| ------ | ---------- |
| `WISP_DATA_DIR/keys/wireguard-key.pem` | SSH/WireGuard private key (`0600` on Linux, icacls on Windows) |
| `WISP_DATA_DIR/wireguard-confs/wg0-client.conf` | Client config fetched from the VM |
| `WISP_LOG_DIR/wisp.log` | Rotating log (5 MB × 3 backups) |

`WISP_DATA_DIR`, `WISP_CONFIG_DIR`, and `WISP_LOG_DIR` are resolved in
`src/wisp/config/constants.py` from [`platformdirs`](https://pypi.org/project/platformdirs/),
so a built wheel works from any directory. On Linux the defaults are
`~/.local/share/wisp` (data), `~/.config/wisp` (config), and
`~/.local/state/wisp/log` (logs). Set the environment variables to override
any of them:

```bash
export WISP_DATA_DIR=~/.local/share/wisp     # keys/ and wireguard-confs/
export WISP_CONFIG_DIR=~/.config/wisp        # wisp.toml, active-session.json
export WISP_LOG_DIR=~/.local/state/wisp/log  # wisp.log
```

## Upgrading

```bash
git pull
uv tool install --editable . --force   # refresh the tool's isolated venv
```

`scripts/install.ps1` (Windows) is idempotent — re-run it to refresh the CLI
and service. On Linux, re-run `sudo wisp daemon install` to refresh the units
and the daemon's runtime venv.

## Uninstall

The quickest path on Linux is the uninstall script (see
[Quick install](#quick-install-linux) for the one-liner). Destroy any live
session first (`wisp destroy <provider>`) — uninstalling
does not tear down cloud resources, and leftovers keep billing (`wisp gc`
finds them afterwards as long as the Pulumi backend is reachable). The
manual steps, should you prefer them:

```bash
# Linux
uv tool uninstall wisp
sudo systemctl disable --now wisp.socket wisp.service
sudo rm -f /usr/lib/systemd/system/wisp.socket /usr/lib/systemd/system/wisp.service
sudo groupdel wisp
rm -rf ~/.config/wisp ~/.local/share/wisp ~/.local/state/wisp
```

```powershell
# Windows
.\scripts\uninstall.ps1
```
