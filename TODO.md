# TODO

Roadmap for Wisp. Work items in rough priority order; anything blocked by
another item is noted.

## Path refactor with `platformdirs` (BLOCKS PyPI)

`src/wisp/config/constants.py` resolves `ROOTDIR` from the current working
directory (walks up for `pyproject.toml`, falls back to `parents[3]`).
Consequences: running `wisp` from another project creates `keys/`,
`wireguard-confs/`, `logs/` and an empty `wisp.toml` there; installed as a
wheel the fallback lands inside the venv; `wireguard-server-install.sh` does
not travel in the wheel.

- [X] `keys/` and `wireguard-confs/` -> `user_data_path("wisp")` (dir 0700)
- [X] `logs/` -> `user_log_path("wisp")`
- [X] `wireguard-server-install.sh` -> package data (`wisp/data/`, loaded via
  `importlib.resources`)
- [X] `platformdirs` declared as an explicit dependency (today it would
  arrive transitively)
- [X] Daemon logging: Linux daemon → journald (stderr at DEBUG, no file, no
  dirs touched); Windows daemon → file under `C:\ProgramData\wisp\logs` via a
  process-local `WISP_LOG_DIR` set by the service wrapper — no machine-wide
  env vars
- [X] Migration path for existing repo-local `keys/`/`logs/` and decision on
  where `wisp.toml` lives (keep XDG user config as-is)
- [X] Until shipped, "run from the repo root" stays documented as a known
  limitation in installation.md

## Publish to PyPI (name: `wisp-vpn`)

Blocked by the path refactor above (a wheel without the server script and
with CWD-dependent paths would misbehave).

- [X] `pyproject.toml`: `name = "wisp-vpn"` (console script stays `wisp`)
- [X] `uv lock` / `uv sync` to regenerate `uv.lock`
- [X] Rename GitHub repo to `wisp-vpn` + `git remote set-url`
- [X] Update `[project.urls]`
- [X] Build check: `uv build` and inspect wheel contents (script present,
  no CWD assumptions)
- [X] First release tag + changelog

## Install bootstrap script (`.sh` / `.ps1`)

Depends on PyPI. Pattern: `curl -fsSL <url>/install.sh | bash`.

- [X] Install `uv` if missing
- [X] `uv tool install wisp-vpn`
- [X] WireGuard tools + Pulumi CLI (or defer to `sudo wisp daemon install`)
- [X] Delegate the privileged part to `sudo wisp daemon install` (single
  source of truth; the bootstrap role is filled by `scripts/install.sh` —
  `setup.sh` was deleted)
- [ ] Windows equivalent (`setup.ps1` parity)

## `wisp doctor` (CLI diagnostics)

Read-only health check; every check already exists as a building block.

- [ ] Daemon: socket reachable + version (Linux: `systemctl status wisp`;
  Windows: `Get-Service WispService`)
- [ ] User in the `wisp` group **and** membership active in the session
- [ ] WireGuard tools present (`wg`, `wg-quick`)
- [ ] Pulumi CLI present + backend authenticated (`pulumi whoami`)
- [ ] Cloud credentials per provider (reuse `validate_*_credentials` pings
  with bounded timeouts)
- [ ] TTL watchdog availability (`systemd-run --user` / `schtasks`)
- [ ] Orphan marker state (warn + point at the cleanup flow)
- [ ] Writable runtime dirs (data/keys/logs once `platformdirs` lands)
- [ ] Checklist output with per-item hints (this becomes the standard first
  step of every bug report)

## `wisp daemon install` (Linux parity with Windows)

The root-requiring daemon setup, owned by the tool (`scripts/install.sh`
delegates to it; the legacy `setup.sh` was deleted).

- [X] `sudo wisp daemon install`: write/refresh systemd units (socket +
  service), create the `wisp` group, enable units, print re-login hint
- [X] `sudo wisp daemon uninstall` parity
- [X] Bootstrap wrapper with no duplicated unit definitions: superseded by
  `scripts/install.sh` (setup.sh removed)

## Watchdog improvements (survive the client machine being off)

Today the TTL watchdog runs on the user's machine (`systemd-run --user` /
`schtasks`): if the machine is off or suspended when the TTL expires, the VM
keeps billing until the next `wisp gc`. README already documents the limit.

- [ ] Analyze cloud-side alternatives per provider (OCI scheduled events /
  Functions, GCP Cloud Scheduler + API trigger, AWS EventBridge) that
  destroy the session server-side
- [ ] Pros/cons: extra cloud permissions vs. the billing risk
- [ ] Possibly make it the default for cloud providers, keeping the local
  watchdog for VPS sessions

## GitHub Pages promo site

Not urgent; publish together with PyPI so the site has an install one-liner
to show.

- [ ] `mkdocs-material` reusing `docs/` (no duplicated content)
- [ ] Landing: hero GIF, feature table, install one-liner
- [ ] Deploy via GitHub Actions on tag

## Misc / nice to have

- [ ] Logo (today the README uses the TUI ASCII banner)
- [ ] Verify `pulumi stack output instance_private_key` shows `[secret]` on
  the next real cloud deploy (secret outputs landed in this cycle)
- [ ] macOS daemon adapter (transport works; privileged ops missing)
- [ ] SSH host-key policy for the VPS provider (`load_system_host_keys` +
  Reject/Warning policy instead of `AutoAddPolicy`)
- [ ] `troubleshooting.md`: keep feeding it — every real debugging session
  belongs there
