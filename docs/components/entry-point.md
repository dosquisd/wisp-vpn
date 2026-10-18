# `wisp.main` — entry point

File: `src/wisp/main.py`. Console script: `wisp = "wisp.main:main"`.

## Types

- `CommandEnum(StrEnum)`: `TUI`, `DEPLOY`, `DESTROY`, `REGIONS`, `GC`,
  `DAEMON`.
- `RuntimeArgs(TypedDict)`: `{command, provider, region, gc_destroy,
  gc_refresh, daemon_command, ttl_minutes, wireguard_port, host, ssh_user,
  ssh_port, ssh_key_path, log_level}`.

## `parse_args(argv=None) -> RuntimeArgs`

Builds an argparse parser:

- A shared `provider_parser` with an optional positional `provider` (choices
  from `ProviderEnum`, default from `get_default_provider()`).
- Subcommands:
  - `deploy` — `-r/--region`, `--ttl MINUTES` (session TTL watchdog override),
    `--wireguard-port PORT`, and VPS-only connection overrides `--host`,
    `--ssh-user`, `--ssh-port`, `--ssh-key-path`.
  - `destroy` — `-r/--region` and VPS-only connection overrides `--host`,
    `--ssh-user`, `--ssh-port`, `--ssh-key-path` (precedence: flags >
    deploy-time marker > `[vps]` TOML).
  - `regions`.
  - `gc` — `--destroy` (tear down leftover stacks) and `--refresh`
    (reconcile state with the cloud before reporting).
  - `daemon` — nested required `install`/`uninstall` subcommands for the
    privileged local daemon.

`deploy` and `destroy` accept `--log-level {debug,info,warning,error}`
(default `info`) for console verbosity — the rotating file log always keeps
`DEBUG`.

Normalizes `command`, `provider`, and `region` to lowercase. If no subcommand is
given, `command` defaults to `CommandEnum.TUI`.

## `_create_provider_with_credentials(provider) -> BaseProvider`

Resolves the provider credentials first (`resolve_aws_credentials()`,
`resolve_oci_credentials()`, `resolve_gcp_credentials()`, or
`resolve_vps_credentials()`) and instantiates the provider from
`PROVIDERS_MAP` with them.

## `_run_gc(destroy, refresh)`

Leftover-stack garbage collection: lists `wisp-stack-*` stacks
(`list_wisp_stacks`), counts real resources per stack via exported state
(`stack_resource_counts` — no program run), optionally refreshes each stack
against the cloud first (`refresh_wisp_stack`), and with `--destroy` calls
`destroy_wisp_stack` per leftover. `_program_for(provider)` builds the
provider's Pulumi program factory — `utils/pulumi.py` must not import
`wisp.providers` (see the cycle note there), so the caller injects it.

## `main() -> None`

Dispatch:

- `GC`: run `_run_gc(destroy, refresh)` and exit.
- `TUI`: run `WispApp().run()` and exit `0`.
- Otherwise: raise the console log handler to `DEBUG`, then:
  - `DAEMON`: **lazily import** `wisp.daemon.install` (a module-level import
    would set `WISP_DAEMON=1` in this process and flip the CLI logger to the
    daemon's stderr-only sink) and `sys.exit(install_daemon())` or
    `sys.exit(uninstall_daemon())`.
  - Then assert a provider is present and instantiate it with resolved
    credentials.
  - `REGIONS`: print `provider.get_available_regions()` as JSON.
  - `DEPLOY`: build a `WispConfig` (`load_wisp_config()`), apply
    `--wireguard-port` when given, and for `vps` overlay the CLI connection
    flags onto the resolved credentials; call `provider.deploy_vm(...)` and
    print the result as JSON. Then, if the TTL is enabled (the `--ttl` flag
    wins over the config), schedule the OS-level watchdog.
  - `DESTROY`: cancel the TTL watchdog, call `provider.delete_vm(...)`, and
    clear the active-session marker when it matches the destroyed
    provider/region.

The `assert` statements are defensive; argparse guarantees a provider in
practice, and the region falls back to the provider configuration.
