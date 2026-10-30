# `wisp.cli`

The Textual terminal UI. For a task-oriented walkthrough see
[usage.md](../usage.md#the-tui); this is a module reference.

## `cli/app.py` — `WispApp(App)`

- Title "Wisp", subtitle "Ephemeral WireGuard VPNs on your own cloud".
- Global CSS (dark theme, cards, primary/secondary/danger buttons).
- Bindings: `q` / `Ctrl+C` → quit.
- Holds an `AppState` instance.
- `on_mount` installs and registers the `main_menu`, `config`, and `deploy`
  screens, then pushes `main_menu`.

## `cli/state.py` — `AppState`

Dataclass of in-memory session state, with TOML-derived defaults:

- `config: WispConfig` — built via `load_wisp_config()` (from the `[general]`
  TOML section).
- `provider_name: str` — from `get_default_provider()` (default `"aws"`).
- `selected_region: str` — default region for the default provider
  (`get_default_region_for(provider)`), falling back to `"us-east-2"`.
- `last_deployment: DeployVMResult | None`
- `aws_credentials` / `oci_credentials` / `gcp_credentials` /
  `vps_credentials` — resolved credentials.
- `session_ttl_minutes: int | None` — per-deployment TTL override set by the
  deploy screen; `None` falls back to `config.max_session_ttl_minutes`.
- `effective_session_ttl()` — the TTL the deploy should schedule (0 = off).
- `get_credentials_for_provider(provider_name)` — returns the resolved
  credentials for the provider (`None` for unknown ones).
- `refresh_credentials()` — re-resolves credentials from TOML/env after config
  changes (calls `reload_toml_config()`).
- `reset_config()` — restores a fresh `WispConfig` from TOML.

## `cli/__main__.py`

Adds the repo root to `sys.path` and runs `WispApp()` — enables
`python -m wisp.cli`.

## `cli/screens/`

| Screen | Responsibility |
| -------- | ---------------- |
| `MainMenuScreen` | Banner, live config summary, orphan warning + cleanup entry, navigation (`1` deploy, `2` settings, `3` exit) |
| `ConfigScreen` | Edits `WispConfig` in memory with inline validation (including `max_session_ttl_minutes`); Save / Reset / Back |
| `DeployScreen` | Provider (`aws`, `oci`, `gcp`, `vps` — see `ProviderEnum` ([`providers/base.py`](../../src/wisp/providers/base.py)) for the current list) + region selection; fetches live regions in a worker thread (repopulates the standard list on failure and drops stale fetches from fast provider switching); swaps the region step for VPS connection fields when `vps` is selected; session TTL watchdog row; pushes `ProgressScreen` |
| `ProgressScreen` | Arms the session guard BEFORE provisioning starts; sets `config.max_session_ttl_minutes` from `effective_session_ttl()`; runs `deploy_vm`/`delete_vm` in a worker thread; writes active-session marker with `ttl_minutes` and computed `expires_at`; schedules local TTL watchdog when enabled; marshals progress to the UI via `call_from_thread`, shows results and a Destroy button; on failure shows a scrollable box with the real Pulumi diagnostics (`describe_pulumi_error` unwraps `exc.command_result.stderr/stdout` and names `ConcurrentUpdateError` explicitly) plus a Destroy Resources button |
| `TunnelScreen` | Live session view. Polls the daemon once per second, classifies health via `health_state()` and `Response.handshake_age_seconds`, and leaves the live view when the remote end stops answering (`SessionEndedScreen`) or another process starts destroying the session (`ExternalTeardownScreen`). Accepts an `adopted` marker for sessions recovered from a previous run |
| `SessionEndedScreen` | Explains that the remote end is gone (in-VM watchdog), offers a state-reconciling cleanup through `ShutdownScreen(orphan=...)`, and returns to the menu |
| `ExternalTeardownScreen` | Shown while another Wisp process destroys the session; polls `teardown-notice.json` and returns to the menu when it clears |

## `cli/session_boot.py`

`arm_session_guard(app, *, provider, region, ttl_minutes, vps=None, write_marker=True,
schedule_watchdog=True)` is the single construction of the teardown closure. Fresh deploys and
startup adoption both go through it so quitting behaves identically either way. Adoption passes
`write_marker=False` and `schedule_watchdog=False`: rewriting the marker would reset the
session's start and expiry, and rescheduling the OS one-shot would push its deadline out every
time the TUI is reopened.

## `cli/app.py` startup adoption

`WispApp._maybe_adopt_live_session()` runs after the first refresh. Adoption is decided by
`_can_adopt(session)` on evidence: the marker's owner must be dead (`process_alive`) and the
daemon must report an interface with a handshake younger than `STALE_TUNNEL_SECONDS`. On
adoption it copies provider/region (and VPS connection details) into `AppState`, arms the
guard, schedules the remaining TTL inside the event loop, and switches to
`TunnelScreen(adopted=session)`. `_offer_orphan_cleanup()` consults the same predicate, so a
tunnel that can be adopted is never also advertised as an orphan to clean up.

## Non-interactive CLI commands

In addition to launching the TUI, `wisp` provides non-interactive CLI subcommands:

| Subcommand | Purpose | Cloud Credentials |
| ---------- | ------- | ----------------- |
| `wisp deploy` | Provisions a VM and connects the local WireGuard client | Required |
| `wisp destroy` | Disconnects client and tears down cloud or VPS resources | Required |
| `wisp connect` | Reconnects local WireGuard client using stored config (`wg0-client.conf`) | Not required (local only) |
| `wisp disconnect` | Panic switch: immediately drops local client interface via daemon | Not required (local only) |
| `wisp regions` | Lists available regions for a provider | Required |
| `wisp gc` | Inspects and cleans up leftover Pulumi stacks | Optional / Required for `--destroy` |
| `wisp daemon` | Installs or uninstalls the privileged local daemon | Root / Admin only |

### Threading note

Long-running provider calls run in Textual `@work(thread=True)` workers. UI
updates from those threads go through `self.app.call_from_thread(...)` to stay on
the UI thread.
