# Wisp Documentation

Wisp provisions **ephemeral WireGuard VPNs on your own cloud**. It spins up a
throwaway virtual machine at a cloud provider, installs and configures a
WireGuard server on it via Paramiko (SSH/SFTP), connects your local machine as
a WireGuard client through a privileged daemon, and tears everything down
again on demand. It can also configure WireGuard directly on a VPS you
already own — no provisioning involved.

Wisp can be driven either through an interactive terminal UI (TUI) or through
non-interactive CLI subcommands.

> **Note:** As of this writing, Wisp ships with support for `aws`, `oci`,
> `gcp`, and `vps` (bring-your-own-server). The provider layer is designed to
> be extensible — new providers can be added by implementing `BaseProvider`
> (or extending `PulumiProvider`) and registering them in `PROVIDERS_MAP`
> ([`src/wisp/providers/__init__.py`](../src/wisp/providers/__init__.py)).
> For the current list, see `ProviderEnum`
> ([`src/wisp/providers/base.py`](../src/wisp/providers/base.py)).

## Documentation index

| Document | Contents |
| ---------- | ---------- |
| [Architecture](./architecture.md) | High-level design, module map, runtime data flow |
| [Installation](./installation.md) | System requirements, `install.sh` (Linux), `setup.ps1` (Windows), daemon, dependencies, uninstall |
| [Usage](./usage.md) | CLI subcommands and the interactive TUI |
| [Configuration](./configuration.md) | `wisp.toml`, `WispConfig`, constants, Pulumi backend |
| [Deployment flow](./deployment-flow.md) | Step-by-step of deploy and destroy |
| [Troubleshooting](./troubleshooting.md) | Field-tested fixes: daemon, sockets, firewalls, leftovers |
| [Components](./components/README.md) | Per-module reference (providers, wireguard, daemon, cli, utils) |
| [Security model](./security.md) | Keys, firewall rules, socket permissions, trust boundaries |
| [Contributing](./contributing.md) | Adding a provider, code layout, style |

## Quick mental model

```text
        ┌─────────────┐  Pulumi Automation API   ┌──────────────────┐
        │  Wisp CLI/  │ ───────────────────────► │  Cloud VM        │
        │  TUI (user) │                          │  (AWS EC2 / OCI  │
        └──────┬──────┘                          │  / GCP Compute)  │
               │                                 └────────┬─────────┘
               │                                          │
               │  Paramiko (SSH/SFTP) runs the            │
               │  WireGuard server installer              │
               │ ────────────────────────────────────────►│
               │                                          │
               │  Fetches generated wg0-client.conf back  │
               │ ◄────────────────────────────────────────│
               │
               │  Unix socket /run/wisp.sock (Linux)
               │  Named Pipe \\.\pipe\wisp (Windows)
               ▼
        ┌──────────────┐  wg-quick up/down wg0    ┌──────────────────┐
        │ wisp daemon  │ ───────────────────────► │ local WireGuard  │
        │ (privileged; │                          │ interface wg0    │
        │ systemd      │                          └──────────────────┘
        │ svc on Win)  │
        └──────────────┘
```

The client-side privileged daemon exists so the unprivileged CLI/TUI never needs
`sudo` to bring the local WireGuard interface up or down; it talks to the daemon
over a group-restricted Unix socket (Linux) or Named Pipe (Windows)
instead.
