# Doctor component (`wisp.doctor`)

The `wisp.doctor` module provides read-only diagnostic checks and report rendering
for the `wisp doctor` CLI command. It verifies every prerequisite required for Wisp
to function on the host machine without modifying files, running `sudo`, or
causing unprompted side effects.

## Module overview

- **Location**: `src/wisp/doctor.py`
- **Entry points**:
  - `run_doctor() -> list[Check]`: Runs all checks and returns structured data.
  - `render_report(checks: Sequence[Check]) -> str`: Formats results into aligned text.
  - `run_doctor_command() -> int`: Prints the report to stdout and returns the exit code (`1` on any failure, `0` otherwise).

## Data model

Diagnostics are modeled using a lightweight, immutable data structure:

```python
class CheckStatus(enum.StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str
    hint: str = ""
```

### Invariants

- **Strictly read-only**: Checks probe paths with `os.access`, `os.stat`, `shutil.which`, and read-only API calls. No directories are created, and no files are written.
- **Fail-safe execution**: Every check is wrapped in `_safe_check()`. Unhandled exceptions are converted to `[fail]` with the exception message as detail, ensuring `run_doctor()` always completes.
- **Actionable hints**: Every `WARN` and `FAIL` check carries a non-empty `hint` specifying the exact command or path to fix the issue. `OK` and `SKIP` checks have empty hints.
- **Exit codes**: Returns `1` only if at least one check is `FAIL`. Warnings and skipped checks return exit code `0`.
- **Severity**: `FAIL` means Wisp cannot work on this machine at all (daemon unreachable, WireGuard tools missing, runtime directories unwritable). Anything tied to a specific provider — Pulumi, `[aws]`/`[oci]`/`[gcp]`/`[vps]` credentials — is `WARN`: a user with a broken AWS key who deploys to OCI or to a Custom VPS must not get a failing exit code.

## Bounded execution and daemon threads

Several validation calls (`validate_aws_credentials`, `validate_oci_credentials`, `validate_gcp_credentials`, and `status_wireguard_client`) perform network or socket I/O without timeout parameters in their underlying client SDKs.

To prevent indefinite hangs, `_run_bounded(func, timeout)` executes probes inside a dedicated `threading.Thread(daemon=True)`:

- **Why daemon threads**: Unlike `concurrent.futures.ThreadPoolExecutor` (whose worker threads join at interpreter exit and block process termination), daemon threads are abruptly terminated when Python shuts down. If a remote endpoint drops packets, Wisp doctor exits promptly when its timer expires.
- **Timeout sentinel**: When `thread.join(timeout)` lapses while the thread remains alive, `_run_bounded` returns `_TIMEOUT_SENTINEL`, which callers map to a `WARN` result with connectivity advice — or to a `FAIL` when the timed-out probe was the daemon itself, since an unanswered daemon is unusable rather than merely slow.

## Diagnostic checks

| Check | Linux behavior | Windows behavior |
|-------|----------------|------------------|
| `daemon` | Checks `/run/wisp.sock` existence and socket mode; probes status over the Unix domain socket. A down interface is reported as `[ok]` ("no tunnel is up"); only an unanswered probe is `[fail]`. | Queries service state via `sc.exe query WispService`; probes status over the Named Pipe `\\.\pipe\wisp`. |
| `membership` | Verifies `wisp` group exists (`grp.getgrnam`) and that current session has active membership (`os.getgroups` or `os.getgid`). | Inspects process token for Administrators SID `S-1-5-32-544` via `whoami /groups`. |
| `wireguard-tools` | Locates `wg` and `wg-quick` via `shutil.which`. | Searches for `wireguard.exe` on `PATH` or under `%ProgramFiles%\WireGuard`. |
| `pulumi` | Verifies `pulumi whoami`. If missing on PATH, checks `~/.pulumi/bin/pulumi`. | Verifies `pulumi whoami`. If missing on PATH, checks `~/.pulumi/bin/pulumi.exe`. |
| `credentials-aws` | Validates STS `GetCallerIdentity` if configured via TOML, env vars, or `~/.aws/`; skips otherwise. | Same as Linux. |
| `credentials-oci` | Validates Identity `list_region_subscriptions` if configured via TOML, env vars, or `~/.oci/config`; skips otherwise. | Same as Linux. |
| `credentials-gcp` | Validates Compute `ListRegions` if configured via TOML, env vars, or ADC file; skips otherwise. | Same as Linux. |
| `credentials-vps` | Validates presence of `[vps].host` and existence of local `ssh_key_path`; skips if host is unset. | Same as Linux. |
| `ttl-watchdog` | Checks `systemd-run` availability if TTL cap > 0; skips if TTL disabled. | Checks `schtasks` availability if TTL cap > 0; skips if TTL disabled. |
| `session-marker` | Reads `active-session.json`. Warns with destroy command if expired or owning PID is dead. | Same as Linux (verifies PID via Windows API). |
| `directories` | Tests writability of runtime roots (`data`, `config`, `log`, `keys`) or their nearest existing ancestors using `os.access`. | Same as Linux. |
