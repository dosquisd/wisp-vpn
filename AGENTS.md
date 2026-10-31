# AGENTS.md

Concise-but-complete context for AI coding sessions (opencode, Claude, etc.)
working on this repository. This exists so you don't have to re-learn the
project from zero every session. Everything here was verified against the
actual codebase — do not contradict it without checking the code first.

## What is Wisp

Wisp provisions **ephemeral WireGuard VPNs on your own cloud**: it spins up a
throwaway VM at a cloud provider, installs and configures a WireGuard server
on it over SSH, connects your local machine as a WireGuard client through a
privileged local daemon, and tears everything down on demand. Driven through
an interactive Textual TUI or non-interactive CLI subcommands.

Three cooperating pieces:

1. **Frontend** — Textual TUI or argparse CLI (unprivileged, runs as your user)
2. **Cloud provider layer** — Pulumi Automation API for provisioning +
   Paramiko SSH/SFTP for remote server configuration
3. **Privileged local daemon** — systemd socket-activated service (Linux) or
   native Windows Service + Named Pipes (Windows)

Supported providers (as of this writing): **AWS**, **OCI**, **GCP**, and
**Custom VPS** (bring-your-own-server, no Pulumi). The provider layer is
extensible — new providers implement `BaseProvider` (or extend
`PulumiProvider`) and register in `PROVIDERS_MAP`. For the current
list, see `ProviderEnum`
([`src/wisp/providers/base.py`](src/wisp/providers/base.py)) and
`PROVIDERS_MAP`
([`src/wisp/providers/__init__.py`](src/wisp/providers/__init__.py)).

## Package layout

```text
src/wisp/
├── main.py                 # Entry point; argparse CLI + TUI dispatch; wisp console script
├── schemas.py              # Shared TypedDicts (InventoryContext: SSH/WireGuard deploy context)
├── cli/                    # Textual TUI
│   ├── app.py              # WispApp (styles, screen registration, startup adoption)
│   ├── state.py            # AppState (in-memory session state, TOML-derived defaults)
│   ├── session_boot.py     # arm_session_guard: one teardown for deploys and adoption
│   ├── __main__.py         # python -m wisp.cli launcher
│   └── screens/            # MainMenu, Config, Deploy, Progress, Tunnel, SessionEnded
├── config/
│   ├── settings.py         # TOML loading (user+project merged), WispConfig, per-provider defaults
│   ├── credentials.py      # Per-provider credential resolution (AWS/OCI/GCP/VPS) + validation
│   └── constants.py        # Paths, tags, Pulumi identity, WireGuard defaults
├── providers/
│   ├── base.py             # BaseProvider ABC + ProviderEnum + DeployVMResult
│   ├── pulumi_base.py      # PulumiProvider: shared deploy/destroy flow for all Pulumi providers
│   ├── aws/                # AWS implementation (provider, pulumi program, constants)
│   ├── oci/                # OCI implementation (provider, pulumi program, constants)
│   ├── gcp/                # GCP implementation (provider, pulumi program, constants)
│   └── vps.py              # Custom VPS (bring-your-own-server): SSH-only, no Pulumi
├── wireguard/
│   ├── remote_server.py    # Remote WireGuard server config via Paramiko (SSH/SFTP)
│   └── local_client.py     # Thin client over the daemon transport
├── daemon/
│   ├── server.py           # asyncio server (runs as root/SYSTEM)
│   ├── transport.py        # Unix socket (Linux) + Named Pipe (Windows) transports
│   ├── adapter.py          # Platform-specific privileged ops (Linux/Windows implemented; macOS NOT)
│   ├── install.py          # `wisp daemon install/uninstall` (systemd units / pywin32 service)
│   ├── protocol.py         # Request/Response wire format (newline-delimited JSON)
│   └── windows_service.py  # Windows Service wrapper (pywin32)
└── utils/                  # logger, platform detection, RNG, public IP, Pulumi helpers,
                            # teardown notice, session TTL watchdog (systemd/schtasks)
```

Supporting non-Python assets at the repo root:

```text
scripts/    install.sh                   # Self-contained bootstrap installer (uv + wisp-vpn + WireGuard
                                         # + optional Pulumi + `daemon install` under sudo + install manifest)
            uninstall.sh                 # Self-contained manifest-driven uninstaller
            wireguard-client-install.sh  # Client installer (self-contained: carries its own shell helpers)
setup.ps1                                # Windows installer: native service, WireGuard exe, Pulumi via winget
packaging/  wisp.socket, wisp.service    # systemd units (Linux only)
wisp.toml.example                        # Config template (wisp.toml is written to WISP_CONFIG_DIR)
```

Non-Python assets shipped **inside** the package:

```text
src/wisp/data/wireguard-server-install.sh  # Non-interactive WireGuard installer (angristan, MIT)
src/wisp/data/wisp.socket                  # systemd socket unit (shipped verbatim)
src/wisp/data/wisp.service.tpl        # systemd service template ({python_executable} placeholder)
```

Runtime artifacts (private key, client config, logs) and `wisp.toml` are
resolved per user via `platformdirs` (env overrides `WISP_DATA_DIR`,
`WISP_CONFIG_DIR`, `WISP_LOG_DIR`) — never relative to the CWD or the repo.

## Conventions (non-negotiable)

- **ALL documentation, docstrings, CLI help strings, code comments, and
  TUI user-facing strings are written in English.**
- **Docstrings follow PEP 257** with Google-style `Args:` / `Returns:` /
  `Raises:` sections. **Code follows PEP 8** as enforced by Ruff.
- **Ruff config** (`pyproject.toml`): line length 88, 4-space indent, double
  quotes, lint rules `E4, E7, E9, F` + import sorting (`I`), target Python 3.14.
- **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `style:`,
  `chore:`, `docs:`. Pull requests against `main`.
- **The user reviews ALL changes BEFORE commits.** Never commit without
  explicit approval. Never push (`git push` is denied by permission rules).
- No emojis in code or docs.
- **Provider list is extensible**: wherever docs enumerate providers, say
  "as of this writing" and point to `ProviderEnum`/`PROVIDERS_MAP` with
  concrete file links — never hard-fix the current list as the only truth.
- **Platform differences are explicit**: wherever behavior differs
  (daemon transport, file permissions, setup scripts), document Linux vs
  Windows separately.

## Key architecture facts (verified against the codebase — do not contradict)

- **SSH keys: RSA 4096** via `pulumi-tls` (`algorithm="RSA", rsa_bits=4096`)
  in ALL providers — **NOT ED25519**. Paramiko loads them supporting
  Ed25519/ECDSA/RSA key formats.
- **Session lifecycle** (`src/wisp/session.py`): a deployed tunnel is billable
  infrastructure, so `SessionGuard` owns the teardown and is **idempotent and
  lock-guarded** — the first trigger wins, later ones are no-ops. Triggers:
  `q`/`Ctrl+C`, `esc`, `d`, `SIGHUP` (terminal closed), `SIGINT`/`SIGTERM`,
  `atexit`, and orphan detection on startup. **The tunnel view is a terminal
  state**: there is deliberately no path back to the menu with a live session,
  because a tunnel nobody watches is a VM nobody tears down. The one exception
  is esc during an in-flight deploy, which is refused because popping the screen
  would let the resources come up with no guard armed. Manual destroy from the
  tunnel view asks first via `ConfirmDestroyScreen` unless `[general]
  confirm_destroy = false` (the dialog's "Don't ask again" persists it).
  Signal handlers must exit explicitly (`os._exit(128 + signum)`) after the
  teardown, since installing a handler replaces the signal's default action.
  The active-session marker is cleared by `teardown_active_session()` once
  `delete_vm` returns (and by CLI `wisp destroy` when the destroy matches the
  marker) — never leave it behind or the next startup "cleans up" an orphan
  whose resources no longer exist.
- **Concurrency is Pulumi's job, not ours.** The stack backend locks a stack for
  the duration of an update, so a second concurrent `up`/`refresh`/`destroy`
  raises `pulumi.automation.errors.ConcurrentUpdateError`. Do NOT add a
  Wisp-owned lock: it would duplicate that guarantee and add a stale-lock
  failure mode. Do handle the error clearly via
  `describe_pulumi_error()` (`src/wisp/utils/pulumi.py`), which also unwraps the
  real diagnostics from `exc.command_result` instead of showing `code: 1`.
- **Startup adoption** (`WispApp._maybe_adopt_live_session`): a marker whose
  owner process is dead *and* whose local interface has a handshake younger than
  `STALE_TUNNEL_SECONDS` is adopted into the tunnel view. Never hijack a marker
  whose owner is alive, and never rewrite the marker or reschedule the OS
  one-shot during adoption (that would silently extend the session).
  `cli/session_boot.arm_session_guard` is the single construction of the
  teardown closure for both fresh deploys and adoption.
- **Remote death is detected, never assumed** (`TunnelScreen.health_state`):
  `local_down` says nothing about the remote end (`wisp disconnect` is local);
  only a handshake older than `STALE_TUNNEL_SECONDS`, or no handshake at all
  after a 180 s grace window, moves the TUI to `SessionEndedScreen`, which
  disarms the guard first. A destroy announces itself through
  `teardown-notice.json` (`utils/operations.py`) so a TUI can show
  `ExternalTeardownScreen` instead of a stale live view; that notice is
  observability only, never mutual exclusion.
- Never capture a screen (`self`) inside a teardown closure: the screen that
  started it is swapped out long before it runs — report progress through
  `app.thread_safe_call` instead.
- **`Ctrl+C` is a key event, NOT a signal.** Textual's driver clears the
  terminal's `ISIG` flag (`LinuxDriver._patch_lflag`), so `Ctrl+C` arrives as a
  keypress on the `ctrl+c` binding. Do not "fix" this with
  `TEXTUAL_ALLOW_SIGNALS`.
- **Textual gotchas (both verified here)**: `Vertical`/`Horizontal` default to
  `height: 1fr` with `overflow: hidden`, so a content-sized wrapper MUST declare
  `height: auto` or it gets squeezed and clips its children. Percentage
  `max-height` resolves against an auto-height parent, so cards go in
  `CenterMiddle`, not `Center`. Textual also focuses the first `Input` before
  the first layout pass, so `scroll_visible` leaves a phantom scroll — re-anchor
  with `call_after_refresh(... scroll_to, y=0)`.
- **Never `query_one` a widget from `compose()`** — children are not mounted
  yet, so it raises `NoMatches`. Pass the value through the call instead (this
  is what broke `DeployScreen`).
- **Provider display names** come from `get_provider_display_name()` in
  `providers/base.py`. Never chain comparisons on the enum value
  (`"AWS" if ... else "OCI"` silently labels GCP as OCI).
- **Remote server configuration is Paramiko SSH/SFTP** (no Ansible, no
  Jinja2): `wireguard/remote_server.py` uploads the packaged
  `src/wisp/data/wireguard-server-install.sh` (a 687-line angristan installer —
  **NOT optional**, it configures the remote server) to `/tmp/` and executes
  it with `sudo` via env vars (`SERVER_PUB_IP`, `SERVER_PORT`, `ALLOWED_IPS`,
  `AUTO_INSTALL=y`, etc.).
- **Region is per-provider**: `[aws].region`, `[oci].region`,
  `[gcp].region` in `wisp.toml` — there is **no cross-provider default
  region**. The CLI resolves the region from `provider.credentials.region`
  when `-r/--region` is omitted.
- **`WispConfig`** (per-session deployment settings) is built from the
  `[general]` TOML section via `load_wisp_config()` — never persisted back.
- **Daemon transport**: Unix socket `/run/wisp.sock` (Linux, systemd socket
  activation via fd 3) or Named Pipe `\\.\pipe\wisp` (Windows). Selected by
  `daemon.transport.get_transport()`.
- **Wisp only touches the WireGuard tunnel it created.** `wg0` is the default
  name of every WireGuard setup and `wg-quick` keeps its config at the same
  `/etc/wireguard/wg0.conf` path, so a blind write would destroy an unrelated
  VPN (and `wg-quick down wg0` would kill it first). Configs Wisp installs
  start with `MANAGED_HEADER` (`# Managed by wisp`); `is_managed()`/
  `foreign_tunnel_reason()` in `daemon/adapter.py` decide ownership, the server
  refuses `connect`/`disconnect` on a foreign tunnel (`refuse_foreign_tunnel`),
  startup `reset_stale_tunnel` skips anything unmanaged, `status` reports no
  handshake age for it, and `Response.tunnel_conflict` lets the CLI and TUI
  refuse **before** provisioning instead of after a billable VM exists. Origin:
  owner decision that coexistence is not desirable — with `wg0` occupied Wisp
  refuses and the user decides, rather than both VPNs fighting over the default
  route. Older Wisp installs wrote no marker, so a leftover config from one
  looks foreign once and must be moved aside manually.
- **The daemon is a separate venv, and mirrors a local checkout**:
  `wisp daemon install` builds `/usr/local/lib/wisp/venv` (root-owned, so a
  root service never executes user-mutable code) from either the PyPI release
  or — when the running CLI's PEP 610 `direct_url.json` points at a local
  directory — a **staged copy of that checkout** (`_daemon_install_target` /
  `_staged_install_target` in `daemon/install.py`; staging keeps the `sudo`
  build from dropping root-owned `build/` and `*.egg-info` into the repo).
  Consequence for development: daemon-side edits do not reach the running
  service until `daemon install` runs again under `sudo` (or a manual
  `uv pip install --force-reinstall --no-deps` into that venv) **and**
  `sudo systemctl restart wisp.service`. Note that a bare `sudo wisp ...`
  fails when sudo's `secure_path` omits `~/.local/bin`; the CLI prints the
  absolute-path form, and `utils/privileges.root_invocation()` builds that
  hint for the daemon messages and the TUI. A stale daemon looks like a client
  bug — e.g. it answers `status` without `handshake_age_seconds`, so startup
  adoption silently never fires.
- **File permissions**: `secure_file()` → `0600` (Linux) or `icacls`
  with well-known SIDs `*S-1-5-18:F` / `*S-1-5-32-544:F` (Windows — SIDs
  because account names are localized on non-English Windows).
- **TOML credentials**: AWS and OCI credentials are **NOT configured in
  `wisp.toml`** — Pulumi deploys use each provider's own credential chain
  (see provider sections below). GCP is fully TOML-configurable.
- **Pulumi stack names are provider-specific**: `get_pulumi_stack_name(provider)`
  → `wisp-stack-aws`, `wisp-stack-oci`, `wisp-stack-gcp` (project
  `wisp-project`).
- **Deploy flow** (shared in `PulumiProvider.deploy_vm`): resolve config →
  `pulumi up` with the provider program → wait `vm_boot_timeout` (default
  60s) → persist RSA key to `WISP_DATA_DIR/keys/wireguard-key.pem` → configure remote
  server (Paramiko SSH/SFTP) → connect local client (daemon). The VPS
  provider (`VPSProvider.deploy_vm`) skips provisioning/boot-wait entirely:
  SSH → configure remote server → connect local client.
- **Destroy flow**: disconnect client → `stack.cancel()` (no-op when nothing
  is in flight) → `stack.destroy()` → remove local artifacts (key + client
  conf). VPS destroy additionally tears the tunnel down on the SERVER: every
  `/etc/wireguard/wisp-*.conf` gets `wg-quick down` (which removes the
  installer's iptables rules), `systemctl disable`, and deletion — packages
  and non-wisp configs are never touched.
- **Session guard is armed BEFORE provisioning starts** (not on success): a
  TUI/console death mid-deploy leaves the active-session marker behind, and
  the signal handlers cancel the in-flight update and destroy partial
  resources. Re-armed unchanged on success and on completed failure.
- **Failed deploys surface real diagnostics**: Pulumi errors are unwrapped
  from `exc.command_result.stderr/stdout` (str(exc) alone is "code: 1") and
  the progress screen shows a scrollable error box with a Destroy Resources
  button — partial resources are billable.
- **Session watchdog (two layers)**:
  - **In-VM layer (primary)**: deployed at `/usr/local/bin/wisp-watchdog` with
    `/etc/wisp/watchdog-<nic>.conf` and templated units `wisp-watchdog@.service`
    / `wisp-watchdog@.timer` (ticks every 60s). It survives local PC power-loss,
    sleep, or TUI death. Triggers on hard TTL (`DEADLINE_EPOCH` computed from
    server clock at install time; 0 disables) or idle timeout (no WireGuard
    handshake for `DEFAULT_WATCHDOG_IDLE_SECONDS = 900` / 15 min; counts from
    deploy time if no peer ever handshook).
  - **Cattle vs pets rule**: on Cloud VMs (`mode=cloud`), watchdog executes
    `systemctl poweroff` (AWS sets `instance_initiated_shutdown_behavior="terminate"`
    to terminate and delete root volume; GCP uses `instance_termination_action="DELETE"`;
    OCI stops instance). On Custom VPS (`mode=vps`), the server is NEVER
    powered off; watchdog strictly tears down Wisp's own interface (`wg-quick down`),
    configs, params (if owned), and units.
  - **Local layer (secondary)** (`src/wisp/utils/watchdog.py`, optional): when
    `[general].max_session_ttl_minutes > 0` (default 60), deploying schedules
    an OS-level one-shot (`systemd-run --user` on Linux, `schtasks` on
    Windows) that runs `wisp destroy <provider> <region>` on the local machine.
    Teardown and `wisp destroy` cancel it. The deploy screen toggles it per run
    (`--ttl` on the CLI).
  - **Keepalive**: `PersistentKeepalive = 25` is automatically added to client
    configs so an idle-but-healthy tunnel emits handshakes and avoids false
    abandonment triggers.
  - **Disarm on manual teardown**: `wisp destroy` / TUI teardown disables
    `wisp-watchdog@<nic>.timer`, removes `/etc/wisp/watchdog-<nic>.conf`, and
    only cleans up the shared script and unit templates when no other session
    configs remain.
  - **Client network fail-safes**: on Windows, `sc.exe config WireGuardTunnel$wg0 start= demand`
    prevents bringing up a dead tunnel after reboot; the daemon runs
    `reset_stale_tunnel` on startup, dropping interfaces with no handshake or
    stale handshakes older than 180s before serving.
  - **Local CLI commands (no cloud credentials)**: `wisp disconnect` is a local
    panic switch bringing down the client interface without touching cloud VMs
    or Pulumi; `wisp connect` re-attaches using the stored client config.
  - **Active session marker**: `active-session.json` records `ttl_minutes` and
    computed `expires_at`. `ActiveSession.is_expired()` and `is_resumable()`
    permit cleanly recognizing an active, unexpired session across restarts.
- **`wisp gc`** inspects leftover `wisp-stack-*` Pulumi stacks via exported
  state (no program run), counts real resources, and with `--destroy` tears
  them down. `--refresh` reconciles state against the cloud first, so
  out-of-band deletions stop counting as leftovers.

## Provider-specific notes

### AWS

- Credentials are NOT in `wisp.toml`: Pulumi and boto3 use the standard AWS
  CLI/SDK chain (env vars, `~/.aws/credentials`, `~/.aws/config`, IAM roles).
  Only `[aws].region` is configurable in the TOML.
- `resolve_aws_credentials()` reads the TOML first, then peeks at
  `~/.aws/{credentials,config}` (network-free, via
  `_read_aws_cli_files()`) — no hardcoded region fallback.
- Regions: `boto3` `ec2.describe_regions()`.
- Resources: security group (UDP wg port + TCP 22) + `t3.micro` EC2 instance,
  most-recent Ubuntu AMI (owner Canonical), RSA 4096 key pair.
- Deployment config follows the [Pulumi AWS installation & configuration
  docs](https://www.pulumi.com/registry/packages/aws/installation-configuration/).

### OCI

- Credentials are NOT in `wisp.toml`: Pulumi and the OCI SDK use the standard
  OCI config chain (`~/.oci/config` profile-based, env vars). Configurable in
  the TOML: `[oci].region`, `[oci].profile` (used for credential
  resolution/validation), and `[oci].compartment_ocid` (which IS passed to
  the Pulumi deploy via `OCIProvider._get_compartment_id()`).
- Regions: OCI Identity API (`list_region_subscriptions`).
- Resources: VCN + Internet Gateway + Route Table + Security List + Subnet +
  Flex Compute Instance (`VM.Standard.A1.Flex`, 2 OCPU, 8 GB, Ubuntu 24.04),
  RSA 4096 key pair.
- Deployment config follows the [Pulumi OCI installation & configuration
  docs](https://www.pulumi.com/registry/packages/oci/installation-configuration/).

### GCP (fully TOML-configurable — was the most problematic provider)

- **Everything in `[gcp]` reaches the deploy**: `project_id`, `region`, and
  `zone` go to the explicit `gcp.Provider` resource;
  `credentials_path` authenticates the deploy as the service account
  (bypassing gcloud/ADC).
- **Credentials**: `credentials_path` points to a service account key JSON
  (Console → IAM & Admin → Service Accounts → Keys → Add Key → JSON) — the
  no-CLI path. Without it, Application Default Credentials (ADC) are used.
- **`gcloud auth application-default login` creates the ADC file** at
  `~/.config/gcloud/application_default_credentials.json` — an
  **authorized_user** JSON of the user's account (NOT a service account).
- **Project fallback**: TOML → `GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` env
  vars → `google.auth.default()` (which returns `(credentials, project)`).
- **Zone derivation**: `get_zone()` returns the explicit zone or derives
  `f"{region}-a"`.
- **Regions**: Compute SDK (`compute_v1.RegionsClient.list` via
  `google-cloud-compute`).

#### GCP gotchas (learned the hard way — all verified)

- `gcp.compute.Firewall` accepts `network`/`project` — **NO `region`**
  (firewall rules are project-level/global).
- **SPOT requires `preemptible=True`**: Pulumi defaults `preemptible` to
  `False` when unset, and GCP rejects `preemptible=false` + `SPOT`
  (Error 400: "contradicting").
- **GCP SSH metadata key is `ssh-keys`** (hyphenated) with the **username as
  a `user:` prefix** (`ubuntu:ssh-rsa AAAA...`). `ssh_authorized_keys` is
  the OCI convention and is **IGNORED by GCP** (breaks SSH auth with
  "Permission denied (publickey)"). Verified in the
  [GCP "Add SSH keys to VMs" docs](https://cloud.google.com/compute/docs/connect/add-ssh-keys).
- **NEVER f-string a `pulumi.Output`** — use `pulumi.Output.concat` or
  `.apply()`. An f-string evaluates before the engine resolves the Output
  and lands the literal "Calling `__str__` on an Output[T] is not supported."
  text in the resource (e.g. the SSH key metadata). Verify deployed
  resources via the SDK when in doubt.
- **`pulumi_gcp` Provider constructor accepts `credentials=<path>`** to the
  SA key JSON — that is how the deploy authenticates without the CLI.
- **Service account keys load with explicit scopes** (`cloud-platform`) —
  a SA JWT without scopes is rejected with `invalid_scope`. Wisp also
  refreshes eagerly so the real cause surfaces (e.g. "Invalid JWT
  Signature" for a revoked/rotated key) instead of a misleading 401.
- **GCP user accounts (Workspace) can be unresolvable** by the Compute APIs
  ("Not found; Gaia id not found for email ...") — use a service account
  key instead of user-account credentials.
- Pulumi's GCP provider also reads `GOOGLE_CREDENTIALS` /
  `GOOGLE_APPLICATION_CREDENTIALS` env vars and gcloud as its own chain —
  see the [Pulumi GCP installation & configuration
  docs](https://www.pulumi.com/registry/packages/gcp/installation-configuration/).

## Docs map

- [`docs/README.md`](docs/README.md) — documentation index
- [`docs/architecture.md`](docs/architecture.md) — high-level design, module map, data flow
- [`docs/installation.md`](docs/installation.md) — requirements, install.sh / setup.ps1, daemon, dependencies
- [`docs/usage.md`](docs/usage.md) — CLI subcommands and the TUI
- [`docs/configuration.md`](docs/configuration.md) — wisp.toml, WispConfig, constants, provider config
- [`docs/deployment-flow.md`](docs/deployment-flow.md) — step-by-step deploy/destroy
- [`docs/security.md`](docs/security.md) — keys, firewall, socket permissions, trust boundaries
- [`docs/contributing.md`](docs/contributing.md) — adding a provider, tooling, style
- [`docs/components/`](docs/components/README.md) — per-module reference (cli, daemon, entry-point, providers, utils, wireguard)

## Tooling commands

```bash
uv sync                                  # install deps (incl. dev group)
uv run ruff check .                      # lint
uv run ruff format .                     # format
uv tool install --editable . --force     # install/refresh the `wisp` CLI
                                         # (REQUIRED after dependency changes —
                                         # the tool has its own venv)
```

Useful verification snippets:

```bash
wisp regions aws        # boto3 regions
wisp regions oci        # OCI Identity API regions
wisp regions gcp        # Compute SDK regions
python -m wisp.cli      # TUI directly
```

When debugging provider auth issues, prefer verifying against the real
resource via the SDK (e.g. `compute_v1.InstancesClient` metadata) rather
than trusting error messages alone — several misleading errors (401 hiding
an invalid JWT signature, `Authentication failed` hiding missing metadata)
were diagnosed exactly that way.
