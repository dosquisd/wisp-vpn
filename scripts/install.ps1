#requires -Version 5.1

<#
.SYNOPSIS
    Installs Wisp on Windows without a repository checkout.

.DESCRIPTION
    Bootstrap installer equivalent to scripts/install.sh. It installs the
    per-user CLI and optional tools first, then elevates only the daemon
    setup. It is safe to download and execute as a single script.

.PARAMETER SkipPulumi
    Do not install Pulumi.

.PARAMETER DryRun
    Print the plan without changing the machine.
#>

[CmdletBinding()]
param(
    [switch]$SkipPulumi,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$WispPackage = "wisp-vpn"
$UvInstallUrl = "https://astral.sh/uv/install.ps1"
$WireGuardInstallerUrl =
    "https://download.wireguard.com/windows-client/wireguard-installer.exe"
$WireGuardInstallerPath = Join-Path $env:TEMP "wisp-wireguard-installer.exe"
$WispDataDir = Join-Path $env:LOCALAPPDATA "wisp"
$ManifestPath = Join-Path $WispDataDir "install-manifest.json"
$WispGroup = "wisp"
$ServiceName = "WispService"

$PreExisting = [System.Collections.Generic.List[string]]::new()
$InstalledByWisp = [System.Collections.Generic.List[string]]::new()
$GroupState = "created_by_wisp"

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Plan {
    param([Parameter(Mandatory)][string]$Message)

    Write-Host "    [dry-run] $Message"
}

function Test-Command {
    param([Parameter(Mandatory)][string]$Name)

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
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

function Repair-BrokenWispTool {
    if ($DryRun) {
        return
    }

    $uv = (Get-Command uv -ErrorAction Stop).Source
    $stdout = Join-Path $env:TEMP "wisp-tool-dir-$([guid]::NewGuid()).log"
    $stderr = Join-Path $env:TEMP "wisp-tool-dir-$([guid]::NewGuid()).err"

    try {
        $process = Start-Process `
            -FilePath $uv `
            -ArgumentList @("tool", "dir") `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr

        $toolRoot = (Get-Content $stdout -Raw -ErrorAction SilentlyContinue).Trim()
        if ($process.ExitCode -ne 0) {
            throw "Could not determine the uv tool directory."
        }
    }
    finally {
        Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }

    if ([string]::IsNullOrWhiteSpace($toolRoot)) {
        throw "Could not determine the uv tool directory."
    }

    $toolPath = Join-Path $toolRoot $WispPackage
    $pythonPath = Join-Path $toolPath "Scripts\python.exe"

    if ((Test-Path $toolPath) -and -not (Test-Path $pythonPath)) {
        Write-Host "Removing the incomplete existing Wisp uv environment."
        Remove-Item -Path $toolPath -Recurse -Force
    }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)

    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Invoke-ElevatedDaemonSetup {
    if ($DryRun) {
        Write-Plan "sudo wisp daemon install"
        return
    }

    $wisp = (Get-Command wisp -ErrorAction Stop).Source
    $escapedWisp = $wisp.Replace("'", "''")
    $daemonStdout = Join-Path $env:TEMP "wisp-daemon-install-$([guid]::NewGuid()).out"
    $daemonStderr = Join-Path $env:TEMP "wisp-daemon-install-$([guid]::NewGuid()).err"
    $daemonFailure = Join-Path $env:TEMP "wisp-daemon-install-$([guid]::NewGuid()).failure"
    $escapedStdout = $daemonStdout.Replace("'", "''")
    $escapedStderr = $daemonStderr.Replace("'", "''")
    $escapedFailure = $daemonFailure.Replace("'", "''")
    $elevatedCommand = @"
`$ErrorActionPreference = 'Stop'
try {
    `$process = Start-Process -FilePath '$escapedWisp' -ArgumentList @('daemon', 'install') -Wait -PassThru -RedirectStandardOutput '$escapedStdout' -RedirectStandardError '$escapedStderr'
    if (`$process.ExitCode -ne 0) {
        throw "wisp daemon install failed with exit code `$(`$process.ExitCode)."
    }
    `$service = Get-Service -Name '$ServiceName' -ErrorAction Stop
    if (`$service.Status -ne 'Running') {
        Start-Service -Name '$ServiceName' -ErrorAction Stop
    }
}
catch {
    `$_ | Out-File -FilePath '$escapedFailure' -Encoding UTF8
    exit 1
}
"@

    $encodedCommand = [Convert]::ToBase64String(
        [Text.Encoding]::Unicode.GetBytes($elevatedCommand)
    )
    $arguments = @(
        "-NoProfile"
        "-ExecutionPolicy"
        "Bypass"
        "-EncodedCommand"
        $encodedCommand
    )

    $process = Start-Process `
        -FilePath "powershell.exe" `
        -Verb RunAs `
        -ArgumentList $arguments `
        -Wait `
        -PassThru

    if (Test-Path $daemonStdout) {
        Get-Content $daemonStdout | ForEach-Object { Write-Host $_ }
    }
    if (Test-Path $daemonStderr) {
        Get-Content $daemonStderr | ForEach-Object { Write-Host $_ }
    }
    if (Test-Path $daemonFailure) {
        Get-Content $daemonFailure | ForEach-Object {
            Write-Host "Elevated daemon setup error: $_" -ForegroundColor Red
        }
    }
    Remove-Item `
        $daemonStdout, `
        $daemonStderr, `
        $daemonFailure `
        -Force `
        -ErrorAction SilentlyContinue

    if ($process.ExitCode -ne 0) {
        throw "Elevated daemon setup failed with exit code $($process.ExitCode)."
    }
}

function Clear-PythonEnvironment {
    foreach ($variable in @("PYTHONHOME", "PYTHONPATH")) {
        if (Test-Path "Env:\$variable") {
            Write-Host "Ignoring inherited $variable for this installation."
            Remove-Item "Env:\$variable" -Force
        }
    }
}

function Install-Uv {
    Write-Step "Checking uv"

    if (Test-Command "uv") {
        Write-Host "uv is already installed."
        $PreExisting.Add("uv")
        return
    }

    if ($DryRun) {
        Write-Plan "download and execute $UvInstallUrl"
        $InstalledByWisp.Add("uv")
        return
    }

    $installer = Invoke-WebRequest `
        -Uri $UvInstallUrl `
        -UseBasicParsing

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -Command $installer.Content

    $env:Path = [Environment]::GetEnvironmentVariable(
        "Path",
        "Machine"
    ) + ";" + [Environment]::GetEnvironmentVariable(
        "Path",
        "User"
    )

    if (-not (Test-Command "uv")) {
        throw "uv installation completed but uv is not on PATH."
    }

    $InstalledByWisp.Add("uv")
}

function Install-WispCli {
    Write-Step "Installing Wisp CLI"

    if ($DryRun) {
        Write-Plan "uv tool install --force --reinstall $WispPackage"
        return
    }

    Repair-BrokenWispTool

    $uv = (Get-Command uv -ErrorAction Stop).Source
    $exitCode = Invoke-NativeCommand `
        -FilePath $uv `
        -ArgumentList @("tool", "install", "--force", "--reinstall", $WispPackage)
    if ($exitCode -ne 0) {
        throw "Could not install $WispPackage with uv."
    }

    $env:Path = [Environment]::GetEnvironmentVariable(
        "Path",
        "Machine"
    ) + ";" + [Environment]::GetEnvironmentVariable(
        "Path",
        "User"
    )

    if (-not (Test-Command "wisp")) {
        throw "Wisp was installed but the wisp command is not on PATH."
    }
}

function Test-WireGuardInstalled {
    $roots = @(
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )

    foreach ($root in $roots) {
        $match = Get-ItemProperty $root -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like "WireGuard*" }
        if ($match) {
            return $true
        }
    }

    return $false
}

function Install-WireGuard {
    Write-Step "Checking WireGuard"

    if (Test-WireGuardInstalled) {
        Write-Host "WireGuard is already installed."
        $PreExisting.Add("wireguard")
        return
    }

    if ($DryRun) {
        Write-Plan "download and execute the WireGuard Windows installer"
        $InstalledByWisp.Add("wireguard")
        return
    }

    Invoke-WebRequest `
        -Uri $WireGuardInstallerUrl `
        -OutFile $WireGuardInstallerPath `
        -UseBasicParsing

    try {
        Start-Process `
            -FilePath $WireGuardInstallerPath `
            -Verb RunAs `
            -Wait
    }
    finally {
        Remove-Item `
            $WireGuardInstallerPath `
            -Force `
            -ErrorAction SilentlyContinue
    }

    if (-not (Test-WireGuardInstalled)) {
        throw "WireGuard installation did not register successfully."
    }

    $InstalledByWisp.Add("wireguard")
}

function Install-Pulumi {
    Write-Step "Checking Pulumi"

    if ($SkipPulumi) {
        Write-Host "Skipping Pulumi."
        return
    }

    if (Test-Command "pulumi") {
        Write-Host "Pulumi is already installed."
        $PreExisting.Add("pulumi")
        return
    }

    if ($DryRun) {
        Write-Plan "winget install --id Pulumi.Pulumi --exact"
        $InstalledByWisp.Add("pulumi")
        return
    }

    if (-not (Test-Command "winget")) {
        throw "winget is required to install Pulumi. Install App Installer first."
    }

    $exitCode = Invoke-NativeCommand `
        -FilePath (Get-Command winget -ErrorAction Stop).Source `
        -ArgumentList @(
            "install"
            "--id"
            "Pulumi.Pulumi"
            "--exact"
            "--accept-source-agreements"
            "--accept-package-agreements"
        )
    if ($exitCode -ne 0) {
        throw "Could not install Pulumi with winget."
    }

    $InstalledByWisp.Add("pulumi")
    Write-Host "Run 'pulumi login' before deploying with a cloud provider."
}

function Write-InstallManifest {
    Write-Step "Writing installation manifest"

    if ($DryRun) {
        Write-Plan "write $ManifestPath"
        return
    }

    New-Item `
        -ItemType Directory `
        -Path $WispDataDir `
        -Force | Out-Null

    $manifest = [ordered]@{
        platform = "windows"
        package = $WispPackage
        pre_existing = @($PreExisting)
        installed_by_wisp = @($InstalledByWisp)
        group = $GroupState
        created_at = [DateTime]::UtcNow.ToString("o")
    }

    $manifest |
        ConvertTo-Json -Depth 3 |
        Set-Content -Path $ManifestPath -Encoding UTF8
}

Clear-PythonEnvironment
Install-Uv
Install-WispCli
Install-WireGuard
Install-Pulumi

if (-not $DryRun) {
    $existingGroup = Get-LocalGroup `
        -Name $WispGroup `
        -ErrorAction SilentlyContinue
    if ($existingGroup) {
        $GroupState = "pre_existing"
    }
}

Invoke-ElevatedDaemonSetup
Write-InstallManifest

Write-Host ""
Write-Host "Wisp installation completed." -ForegroundColor Green
Write-Host "Sign out and back in for the 'wisp' group membership to take effect."
