# `wisp.cli`

The command-line interface and argument parser for Wisp. For the Textual TUI,
see [`wisp.tui`](./tui.md).

## Package structure

The CLI package is organized into modular components:

- `wisp.cli.args`: defines `CommandEnum` (the available CLI subcommands) and
  the `RuntimeArgs` TypedDict.
- `wisp.cli.options`: reusable argument definitions (`add_provider`,
  `add_log_level`, `add_region`, `add_vps_options`) and `apply_log_level`.
- `wisp.cli.commands`: modular subcommands implementing `register(subparsers)`
  and `run(args) -> int`.
- `wisp.cli.parser`: parser construction (`build_parser`), normalization
  (`parse_args`), and CLI execution (`main`).
- `wisp.cli.__main__`: entry point for running `python -m wisp.cli`.

## Commands

| Subcommand | Module | Purpose | Cloud Credentials |
| ---------- | ------ | ------- | ----------------- |
| `wisp deploy` | `wisp.cli.commands.deploy` | Provisions a VM and connects the local WireGuard client | Required |
| `wisp destroy` | `wisp.cli.commands.destroy` | Disconnects client and tears down cloud or VPS resources | Required |
| `wisp connect` | `wisp.cli.commands.connect` | Reconnects local WireGuard client using stored config (`wg0-client.conf`) | Not required (local only) |
| `wisp disconnect` | `wisp.cli.commands.disconnect` | Panic switch: immediately drops local client interface via daemon | Not required (local only) |
| `wisp regions` | `wisp.cli.commands.regions` | Lists available regions for a provider | Required |
| `wisp gc` | `wisp.cli.commands.gc` | Inspects and cleans up leftover Pulumi stacks | Optional / Required for `--destroy` |
| `wisp daemon` | `wisp.cli.commands.daemon` | Installs or uninstalls the privileged local daemon | Root / Admin only |
| `wisp doctor` | `wisp.cli.commands.doctor` | Runs system diagnostics and prerequisite checks | Not required |
