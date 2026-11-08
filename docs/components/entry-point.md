# `wisp.main` — entry point

File: `src/wisp/main.py`. Console script: `wisp = "wisp.main:main"`.

`wisp.main` is a thin entry point that delegates directly to `wisp.cli.parser.main()`.
It exposes `main` for console script execution, Python module execution
(`python -m wisp.main`), and internal interpreter re-entry (such as the TTL
watchdog bootstrap).

## Structure

- **Module entry**: imports `main` from `wisp.cli.parser`.
- **Top-level execution**: runs `sys.exit(main())` when invoked as `__main__`.

For CLI argument parsing, option definitions, and command dispatch, see
[`wisp.cli`](./cli.md). For shared deployment and teardown orchestration, see
`wisp.services`.
