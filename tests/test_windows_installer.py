"""Exercise the Windows bootstrap's elevation without installing a service."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell.exe")
INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL, reason="Windows PowerShell is required"
)

# Load function definitions through the AST, leaving the bootstrap unexecuted.
# Both process boundaries are replaced; the generated elevated script still
# runs in a real PowerShell child so quoting, redirection and exit codes apply.
HARNESS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:WISP_TEST_INSTALLER, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count) { throw $parseErrors[0] }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
$DryRun = $env:WISP_TEST_MODE -eq 'dry-run'
$WispDataDir = $env:WISP_TEST_DATA
function Test-Administrator { return $env:WISP_TEST_MODE -like 'admin-*' }
function Get-Command {
    param($Name, $ErrorAction)
    if ($DryRun) { throw 'Dry run must not resolve an executable.' }
    return [pscustomobject]@{ Source = $env:WISP_TEST_EXECUTABLE }
}
$mockProcess = @'
function Start-Process {
    param(
        $FilePath, $ArgumentList, $Verb, $WindowStyle,
        [switch]$Wait, [switch]$PassThru,
        $RedirectStandardOutput, $RedirectStandardError
    )
    if ($WindowStyle -ne 'Hidden' -or -not $Wait -or -not $PassThru) {
        throw 'Expected a hidden, synchronous process with an exit code.'
    }
    if ($Verb) {
        if ($Verb -ne 'RunAs') { throw 'Expected RunAs.' }
        if ($env:WISP_TEST_MODE -eq 'cancel') {
            throw [System.Reflection.TargetInvocationException]::new(
                'Wrapped launch failure',
                [System.ComponentModel.Win32Exception]::new(1223)
            )
        }
        if ($env:WISP_TEST_MODE -eq 'launch-error') {
            throw 'Cannot launch PowerShell.'
        }
        $encoded = $ArgumentList[$ArgumentList.Count - 1]
        $command = [Text.Encoding]::Unicode.GetString(
            [Convert]::FromBase64String($encoded)
        )
        $child = [Convert]::ToBase64String(
            [Text.Encoding]::Unicode.GetBytes($script:mockProcess + "`n" + $command)
        )
        & $FilePath -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand $child
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE }
    }
    if ($FilePath -ne $env:WISP_TEST_EXECUTABLE -or
        ($ArgumentList -join ' ') -ne 'daemon install') {
        throw 'The original absolute Wisp path or arguments were lost.'
    }
    if ($env:WISP_TEST_MODE -eq 'child-error') {
        throw 'Cannot launch Wisp.'
    }
    [IO.File]::WriteAllText($RedirectStandardOutput, "daemon output`n")
    [IO.File]::WriteAllText($RedirectStandardError, "daemon diagnostic`n")
    $code = if ($env:WISP_TEST_MODE -like '*failure') { 7 } else { 0 }
    return [pscustomobject]@{ ExitCode = $code }
}
'@
. ([scriptblock]::Create($mockProcess))
try { Invoke-ElevatedDaemonSetup }
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""


@pytest.mark.parametrize(
    ("mode", "exit_code", "expected"),
    [
        ("dry-run", 0, "[dry-run]"),
        ("admin-success", 0, "daemon output"),
        ("admin-failure", 1, "failed with exit code 7"),
        ("success", 0, "daemon output"),
        ("failure", 1, "failed with exit code 7"),
        ("cancel", 1, "canceled or denied (Windows error 1223)"),
        ("launch-error", 1, "Could not request Administrator permission"),
        ("child-error", 1, "Cannot launch Wisp."),
    ],
)
def test_daemon_setup(tmp_path: Path, mode: str, exit_code: int, expected: str) -> None:
    """Check UAC errors, child failures and paths containing apostrophes/spaces."""
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    data_dir = tmp_path / "User's data"
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
        ],
        env={
            **os.environ,
            "WISP_TEST_INSTALLER": str(INSTALLER),
            "WISP_TEST_MODE": mode,
            "WISP_TEST_DATA": str(data_dir),
            "WISP_TEST_EXECUTABLE": str(tmp_path / "User's tools" / "wisp.exe"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == exit_code, output
    assert expected in output, output
    assert "CLIXML" not in output
    if mode in ("success", "failure"):
        assert "daemon diagnostic" in output
        logs = list((data_dir / "installer-logs").glob("*.log"))
        assert len(logs) == 2
        if mode == "failure":
            assert "Installation logs:" in output
            assert all(str(log) in output for log in logs)
    if mode == "dry-run":
        assert not data_dir.exists()
