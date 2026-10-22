#requires -Version 5.1

<#
.SYNOPSIS
    Removes the Wisp installation from Windows.

.DESCRIPTION
    Windows counterpart of scripts/uninstall.sh. Wisp-owned files, the CLI,
    daemon, group, user data, and dependencies recorded as installed by Wisp
    are removed in one run. Pre-existing dependencies are left untouched.

.PARAMETER DryRun
    Print the actions without changing the machine.

.PARAMETER Yes
    Do not prompt before removing dependencies installed by Wisp.
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

$WispPackage = "wisp-vpn"
$WispGroup = "wisp"
$ServiceName = "WispService"
$WispInstallDir = Join-Path $env:ProgramData "wisp"
$WispDataDir = Join-Path $env:LOCALAPPDATA "wisp"
$WispRoamingDir = Join-Path $env:APPDATA "wisp"
$ManifestPath = Join-Path $WispDataDir "install-manifest.json"

$Manifest = $null
if (Test-Path $ManifestPath) {
    try {
        $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        Write-Warning "Could not read the Wisp installation manifest."
    }
}

$InstalledByWisp = @()
if ($Manifest -and $Manifest.installed_by_wisp) {
    $InstalledByWisp = @($Manifest.installed_by_wisp)
}

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Plan {
    param([Parameter(Mandatory)][string]$Message)

    Write-Host "    [dry-run] $Message"
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList
    )

    $stdout = Join-Path $env:TEMP "wisp-stdout-$([guid]::NewGuid()).log"
    $stderr = Join-Path $env:TEMP "wisp-stderr-$([guid]::NewGuid()).log"

    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr

        if (Test-Path $stdout) {
            Get-Content $stdout | ForEach-Object { Write-Host $_ }
        }
        if (Test-Path $stderr) {
            Get-Content $stderr | ForEach-Object { Write-Host $_ }
        }

        return $process.ExitCode
    }
    finally {
        Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

function Confirm-Removal {
    param([Parameter(Mandatory)][string]$Message)

    if ($Yes -or $DryRun) {
        return $true
    }

    $answer = Read-Host "$Message [y/N]"
    return $answer -in @("y", "Y", "yes", "YES")
}

function Invoke-ElevatedMachineCleanup {
    Write-Step "Privileged daemon and machine-wide files"

    $wispCommand = Get-Command wisp -ErrorAction SilentlyContinue
    $escapedWisp = ""
    if ($wispCommand) {
        $escapedWisp = $wispCommand.Source.Replace("'", "''")
    }

    $removeGroup = $false
    if ($Manifest -and $Manifest.group -eq "created_by_wisp") {
        $removeGroup = $true
    }
    $removeWireGuard = "wireguard" -in $InstalledByWisp
    $groupLiteral = if ($removeGroup) { '$true' } else { '$false' }
    $wireGuardLiteral = if ($removeWireGuard) { '$true' } else { '$false' }
    $cleanupFailure = Join-Path $env:TEMP "wisp-daemon-uninstall-$([guid]::NewGuid()).failure"

    if ($DryRun) {
        Write-Plan "sudo wisp daemon uninstall"
        Write-Plan "remove $WispInstallDir"
        if ($removeGroup) {
            Write-Plan "remove the '$WispGroup' group"
        }
        if ($removeWireGuard) {
            Write-Plan "uninstall WireGuard with winget"
        }
        return
    }

    if ([string]::IsNullOrWhiteSpace($escapedWisp)) {
        Write-Warning "'wisp' is not on PATH; cannot run 'wisp daemon uninstall'."
    }

    $elevatedCommand = @"
`$ErrorActionPreference = 'Stop'
`$wisp = '$escapedWisp'
try {
    if (-not `$wisp) {
        throw "'wisp' is not on PATH in the elevated process."
    }
    `$service = Get-Service -Name '$ServiceName' -ErrorAction SilentlyContinue
    if (`$service -and `$service.Status -ne 'Stopped') {
        `$stop = & sc.exe stop '$ServiceName' 2>&1
        if (`$LASTEXITCODE -ne 0) {
            Write-Warning "Could not stop '$ServiceName'; continuing with daemon removal."
        }
        else {
            `$service.Refresh()
            `$service.WaitForStatus(
                [System.ServiceProcess.ServiceControllerStatus]::Stopped,
                [TimeSpan]::FromSeconds(30)
            )
        }
    }
    `$stdout = Join-Path `$env:TEMP 'wisp-daemon-uninstall-out.log'
    `$stderr = Join-Path `$env:TEMP 'wisp-daemon-uninstall-err.log'
    `$process = Start-Process -FilePath `$wisp -ArgumentList @('daemon', 'uninstall') -Wait -PassThru -RedirectStandardOutput `$stdout -RedirectStandardError `$stderr
    if (Test-Path `$stdout) { Get-Content `$stdout | ForEach-Object { Write-Host `$_ } }
    if (Test-Path `$stderr) { Get-Content `$stderr | ForEach-Object { Write-Host `$_ } }
    if (`$process.ExitCode -ne 0) {
        throw "wisp daemon uninstall failed with exit code `$(`$process.ExitCode)."
    }
    if (Test-Path '$WispInstallDir') {
        Remove-Item -Path '$WispInstallDir' -Recurse -Force
    }
    if ($groupLiteral) {
        `$group = Get-LocalGroup -Name '$WispGroup' -ErrorAction SilentlyContinue
        if (`$group) {
            `$members = @(Get-LocalGroupMember -Group '$WispGroup' -ErrorAction SilentlyContinue)
            if (`$members.Count -eq 0) {
                Remove-LocalGroup -Name '$WispGroup'
            }
            else {
                Write-Warning "Keeping '$WispGroup': it still has members."
            }
        }
    }
    if ($wireGuardLiteral -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        `$output = & winget uninstall --id 'WireGuard.WireGuard' --exact --silent 2>&1
        `$output | ForEach-Object { Write-Host `$_ }
        if (`$LASTEXITCODE -ne 0) {
            Write-Warning 'Could not uninstall WireGuard with winget.'
        }
    }
}
catch {
    `$_ | Out-File -FilePath '$cleanupFailure' -Encoding UTF8
    exit 1
}
"@

    $encodedCommand = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes($elevatedCommand)
    )
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -Verb RunAs `
        -ArgumentList @(
            "-NoProfile"
            "-ExecutionPolicy"
            "Bypass"
            "-EncodedCommand"
            $encodedCommand
        ) `
        -Wait `
        -PassThru

    if (Test-Path $cleanupFailure) {
        Get-Content $cleanupFailure | ForEach-Object {
            Write-Host "Privileged Wisp cleanup error: $_" -ForegroundColor Red
        }
        Remove-Item $cleanupFailure -Force -ErrorAction SilentlyContinue
    }
    if ($process.ExitCode -ne 0) {
        throw "Privileged Wisp cleanup failed with exit code $($process.ExitCode)."
    }
}

function Remove-WispCli {
    Write-Step "Wisp CLI ($WispPackage)"

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Warning "uv is not on PATH; cannot uninstall $WispPackage."
        return
    }

    if ($DryRun) {
        Write-Plan "uv tool uninstall $WispPackage"
        return
    }

    $exitCode = Invoke-NativeCommand `
        -FilePath (Get-Command uv -ErrorAction Stop).Source `
        -ArgumentList @("tool", "uninstall", $WispPackage)
    if ($exitCode -ne 0) {
        throw "Could not uninstall $WispPackage with uv."
    }
}

function Remove-Pulumi {
    if ("pulumi" -notin $InstalledByWisp) {
        return
    }

    Write-Step "Pulumi"
    if (-not (Confirm-Removal "Remove Pulumi?")) {
        Write-Host "Kept Pulumi."
        return
    }

    if ($DryRun) {
        Write-Plan "winget uninstall --id Pulumi.Pulumi --exact"
        return
    }

    $exitCode = Invoke-NativeCommand `
        -FilePath (Get-Command winget -ErrorAction Stop).Source `
        -ArgumentList @("uninstall", "--id", "Pulumi.Pulumi", "--exact", "--silent")
    if ($exitCode -ne 0) {
        throw "Could not uninstall Pulumi with winget."
    }
}

function Remove-UserData {
    Write-Step "Wisp user data"

    foreach ($path in @($WispDataDir, $WispRoamingDir)) {
        if (-not (Test-Path $path)) {
            continue
        }

        if ($DryRun) {
            Write-Plan "remove $path"
        }
        else {
            Remove-Item -Path $path -Recurse -Force
        }
    }
}

function Remove-Uv {
    if ("uv" -notin $InstalledByWisp) {
        return
    }

    Write-Step "uv"
    if (-not (Confirm-Removal "Remove uv?")) {
        Write-Host "Kept uv."
        return
    }

    if ($DryRun) {
        Write-Plan "uv self uninstall"
        return
    }

    $exitCode = Invoke-NativeCommand `
        -FilePath (Get-Command uv -ErrorAction Stop).Source `
        -ArgumentList @("self", "uninstall")
    if ($exitCode -ne 0) {
        throw "Could not uninstall uv."
    }
}

if ($DryRun) {
    Write-Host "Dry run: nothing will be changed."
}

Invoke-ElevatedMachineCleanup
Remove-WispCli
Remove-Pulumi
Remove-UserData
Remove-Uv

Write-Host ""
Write-Host "Wisp uninstallation completed." -ForegroundColor Green
