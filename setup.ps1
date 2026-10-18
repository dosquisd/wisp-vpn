#requires -Version 5.1

$ErrorActionPreference = "Stop"

$WispGroup = "wisp"

$WispInstallDir = Join-Path $env:ProgramData "wisp"
$WispSourceDir = Join-Path $WispInstallDir "src"
$WispVenvDir = Join-Path $WispInstallDir "venv"
$WispLogsDir = Join-Path $WispInstallDir "logs"

$WireGuardInstallerUrl =
    "https://download.wireguard.com/windows-client/wireguard-installer.exe"

$WireGuardInstallerPath =
    Join-Path $env:TEMP "wireguard-installer.exe"

$ProjectRoot = $PSScriptRoot


function Write-Step {
    param(
        [Parameter(Mandatory)]
        [string]$Message
    )

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}


function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)

    $isAdministrator = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )

    $integrityLevel = (whoami /groups /fo csv | Select-String -Pattern `
        'S-1-16-(12288|16384)'
    )

    $isElevated = $null -ne $integrityLevel

    if (-not $isAdministrator -or -not $isElevated) {
        Write-Host "Requesting administrator privileges..."

        $arguments = @(
            "-NoProfile"
            "-ExecutionPolicy"
            "Bypass"
            "-File"
            $PSCommandPath
        )

        $elevatedProcess = Start-Process `
            -FilePath "powershell.exe" `
            -Verb RunAs `
            -ArgumentList $arguments `
            -Wait `
            -PassThru

        exit $elevatedProcess.ExitCode
    }
}


function Test-Command {
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    return $null -ne (
        Get-Command $Name -ErrorAction SilentlyContinue
    )
}


function Clear-PythonEnvironment {
    # A machine-wide PYTHONHOME or PYTHONPATH poisons every Python process,
    # including the uv-managed interpreter that builds Wisp. The interpreter
    # then loads a foreign standard library and fails with
    # "SRE module mismatch". Drop both so every child process inherits a
    # clean environment; Python locates its own standard library without
    # them.
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
        return
    }

    Write-Host "uv is required to create the daemon environment."

    $installer = Invoke-WebRequest `
        -Uri "https://astral.sh/uv/install.ps1" `
        -UseBasicParsing

    & powershell `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -Command $installer.Content

    # uv's installer may add its location to PATH only for future
    # processes, so refresh PATH here.
    $env:Path = [Environment]::GetEnvironmentVariable(
        "Path",
        "Machine"
    ) + ";" + [Environment]::GetEnvironmentVariable(
        "Path",
        "User"
    )

    if (-not (Test-Command "uv")) {
        throw "uv installation completed but the executable could not be found."
    }
}


function Install-Pulumi {
    Write-Step "Installing Pulumi"

    if (Test-Command "pulumi") {
        Write-Host "Pulumi is already installed."
        return
    }

    winget install `
        pulumi `
        --accept-source-agreements `
        --accept-package-agreements
}


function Install-WireGuard {
    Write-Step "Installing WireGuard"

    # The installer registers the WireGuard application/services.
    # Checking the uninstall registry is more reliable than depending
    # on a command being available in PATH.
    $wireGuardInstalled = Get-ItemProperty `
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" `
        -ErrorAction SilentlyContinue |
        Where-Object {
            $_.DisplayName -like "WireGuard*"
        }

    if (-not $wireGuardInstalled) {
        $wireGuardInstalled = Get-ItemProperty `
            "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*" `
            -ErrorAction SilentlyContinue |
            Where-Object {
                $_.DisplayName -like "WireGuard*"
            }
    }

    if ($wireGuardInstalled) {
        Write-Host "WireGuard is already installed."
        return
    }

    Invoke-WebRequest `
        -Uri $WireGuardInstallerUrl `
        -OutFile $WireGuardInstallerPath `
        -UseBasicParsing

    try {
        Start-Process `
            -FilePath $WireGuardInstallerPath `
            -Wait `
            -NoNewWindow
    }
    finally {
        Remove-Item `
            $WireGuardInstallerPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
}


function Initialize-WispDirectories {
    Write-Step "Creating Wisp directories"

    New-Item `
        -ItemType Directory `
        -Path $WispInstallDir `
        -Force | Out-Null

    New-Item `
        -ItemType Directory `
        -Path $WispSourceDir `
        -Force | Out-Null

    New-Item `
        -ItemType Directory `
        -Path $WispLogsDir `
        -Force | Out-Null
}


function Initialize-WispGroup {
    Write-Step "Configuring Wisp group"

    $group = Get-LocalGroup `
        -Name $WispGroup `
        -ErrorAction SilentlyContinue

    if (-not $group) {
        New-LocalGroup `
            -Name $WispGroup `
            -Description "Wisp daemon users" |
            Out-Null
    }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $userName = $identity.Name

    $members = Get-LocalGroupMember `
        -Group $WispGroup `
        -ErrorAction SilentlyContinue

    $isMember = $members | Where-Object {
        $_.SID.Value -eq $identity.User.Value
    }

    if (-not $isMember) {
        Add-LocalGroupMember `
            -Group $WispGroup `
            -Member $userName
    }

    Write-Host "User '$userName' is a member of '$WispGroup'."
}


function Repair-WispDirectoryAccess {
    Write-Step "Repairing Wisp directory access"

    if (-not (Test-Path $WispInstallDir)) {
        return
    }

    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $takeownDefault = if ((Get-Culture).TwoLetterISOLanguageName -eq "es") {
        "S"
    }
    else {
        "Y"
    }

    takeown.exe `
        /F $WispInstallDir `
        /R `
        /D $takeownDefault | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Could not take ownership of the existing Wisp directory."
    }

    icacls $WispInstallDir `
        /reset `
        /T `
        /C | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Could not reset permissions on the existing Wisp directory."
    }

    icacls $WispInstallDir `
        /grant "*${userSid}:(F)" `
        /T `
        /C | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Could not restore access to the existing Wisp directory."
    }
}


function Install-WispSource {
    Write-Step "Installing Wisp source"

    Copy-Item `
        -Path (Join-Path $ProjectRoot "src\*") `
        -Destination $WispSourceDir `
        -Recurse `
        -Force

    $deployedEggInfo = Join-Path $WispSourceDir "wisp.egg-info"

    if (Test-Path $deployedEggInfo) {
        Remove-Item `
            $deployedEggInfo `
            -Recurse `
            -Force
    }

    Copy-Item `
        -Path (Join-Path $ProjectRoot "pyproject.toml") `
        -Destination $WispInstallDir `
        -Force

    Copy-Item `
        -Path (Join-Path $ProjectRoot "README.md") `
        -Destination $WispInstallDir `
        -Force

    Copy-Item `
        -Path (Join-Path $ProjectRoot "uv.lock") `
        -Destination $WispInstallDir `
        -Force
}


function Install-WispPythonEnvironment {
    Write-Step "Creating Wisp Python environment"

    if (Test-Path $WispVenvDir) {
        Write-Host "Removing existing virtual environment..."
        Remove-Item `
            $WispVenvDir `
            -Recurse `
            -Force
    }

    & uv venv `
        $WispVenvDir `
        --python 3.14

    $python = Join-Path $WispVenvDir "Scripts\python.exe"

    if (-not (Test-Path $python)) {
        throw "Python executable was not created: $python"
    }

    Write-Host "Installing locked Python dependencies..."

    $requirements = Join-Path $env:TEMP "wisp-requirements.txt"

    try {
        & uv export `
            --directory $WispInstallDir `
            --frozen `
            --no-dev `
            --no-emit-project `
            --no-hashes `
            --format requirements.txt |
            Set-Content `
                -Path $requirements `
                -Encoding UTF8

        if ($LASTEXITCODE -ne 0) {
            throw "Could not export locked dependencies from uv.lock."
        }

        if (-not (Test-Path $requirements)) {
            throw "Could not create the dependency requirements file."
        }

        $content = Get-Content $requirements

        if ([string]::IsNullOrWhiteSpace(
            ($content -join "")
        )) {
            throw "The exported dependency requirements file is empty."
        }

        Write-Host "Installing locked dependencies..."

        & uv pip install `
            --python $python `
            -r $requirements

        if ($LASTEXITCODE -ne 0) {
            throw "Could not install locked Python dependencies."
        }

        Write-Host "Installing Wisp into the virtual environment..."

        & uv pip install `
            --python $python `
            --no-deps `
            $WispInstallDir

        if ($LASTEXITCODE -ne 0) {
            throw "Could not install Wisp into the virtual environment."
        }

    }
    finally {
        Remove-Item `
            $requirements `
            -Force `
            -ErrorAction SilentlyContinue
    }
}


function Set-WispDirectoryPermissions {
    Write-Step "Configuring Wisp directory permissions"

    icacls $WispInstallDir `
        /inheritance:r `
        /T `
        /C | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure Wisp directory permissions."
    }

    # Apply explicit permissions to every object because protected child ACLs
    # do not reliably receive inherited entries from the installation root.
    icacls $WispInstallDir `
        /grant:r `
            "SYSTEM:(F)" `
            "*S-1-5-32-544:(F)" `
            "${WispGroup}:(RX)" `
        /T `
        /C | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Could not apply Wisp directory permissions."
    }

    Write-Host "Wisp directory permissions configured."
}


function Install-WispService {
    Write-Step "Installing Wisp Windows Service"

    $python = Join-Path $WispVenvDir "Scripts\python.exe"
    $pywin32SystemDir = Join-Path `
        $WispVenvDir `
        "Lib\site-packages\pywin32_system32"

    Get-ChildItem `
        -Path $pywin32SystemDir `
        -Filter "*.dll" `
        -File |
        Copy-Item `
            -Destination $WispVenvDir `
            -Force

    & $python `
        -m wisp.daemon.windows_service `
        install

    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the Wisp Windows service."
    }

    & $python `
        -m wisp.daemon.windows_service `
        --startup auto `
        update

    if ($LASTEXITCODE -ne 0) {
        throw "Could not update the Wisp Windows service."
    }

    $pythonBaseDir = (& $python -c "import sys; print(sys.base_prefix)").Trim()
    $machinePath = [Environment]::GetEnvironmentVariable(
        "Path",
        "Machine"
    )

    if ([string]::IsNullOrWhiteSpace($pythonBaseDir)) {
        throw "Could not determine the base Python directory."
    }

    New-ItemProperty `
        -Path "HKLM:\SYSTEM\CurrentControlSet\Services\WispService" `
        -Name "Environment" `
        -PropertyType MultiString `
        -Value @(
            "PATH=$pythonBaseDir;$machinePath"
        ) `
        -Force | Out-Null
}


function Start-WispService {
    Write-Step "Starting Wisp service"

    $service = Get-Service `
        -Name "WispService" `
        -ErrorAction SilentlyContinue

    if (-not $service) {
        throw "WispService was not installed."
    }

    if ($service.Status -ne "Running") {
        Start-Service -Name "WispService"
    }
}


function Stop-WispServiceIfRunning {
    Write-Step "Stopping existing Wisp service"

    $service = Get-Service -Name "WispService" -ErrorAction SilentlyContinue

    if (-not $service) {
        return
    }

    if ($service.Status -ne "Stopped") {
        Stop-Service -Name "WispService" -Force

        $service.WaitForStatus("Stopped", (New-TimeSpan -Seconds 15))
    }
}


function Uninstall-WispServiceIfExists {
    Write-Step "Removing existing Wisp service registration"

    $service = Get-Service -Name "WispService" -ErrorAction SilentlyContinue

    if (-not $service) {
        return
    }

    $python = Join-Path $WispVenvDir "Scripts\python.exe"

    if (Test-Path $python) {
        & $python -m wisp.daemon.windows_service remove
    }
    else {
        # Fallback if the venv is already gone/corrupted.
        sc.exe delete WispService | Out-Null
    }
}


Test-Administrator

Clear-PythonEnvironment

Install-Uv
Install-Pulumi
Install-WireGuard

Stop-WispServiceIfRunning
Uninstall-WispServiceIfExists

Initialize-WispDirectories
Initialize-WispGroup
Repair-WispDirectoryAccess
Install-WispSource
Install-WispPythonEnvironment

Install-WispService
Set-WispDirectoryPermissions
Start-WispService

Write-Host ""
Write-Host "Wisp installation completed." -ForegroundColor Green
Write-Host ""
Write-Host "The current user was added to the 'wisp' group." -ForegroundColor Yellow
Write-Host "Sign out and back in for the new group membership to take effect."
