<#
.SYNOPSIS
  Local AI Overlay Pro installer for Windows.

.DESCRIPTION
  One-liners (PowerShell):

    Overlay (desktop HUD + settings/dashboard):
      irm https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.ps1 | iex

    Agent only (share this machine's Ollama status with overlays elsewhere):
      & ([scriptblock]::Create((irm https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.ps1))) -Agent

    Uninstall:
      & ([scriptblock]::Create((irm https://raw.githubusercontent.com/nomad9021/Local-AI-Overlay-Pro/main/install/install.ps1))) -Uninstall

  No admin rights needed. Installs to %LOCALAPPDATA%\LocalAIOverlayPro (app + its
  own Python venv); settings live in %APPDATA%\LocalAIOverlayPro and survive
  reinstalls. Python 3.9+ is installed with winget if it's missing.
  Run from an elevated prompt with -Agent and the agent is registered as a
  startup task (runs before anyone logs in) and the firewall port is opened.
#>
param(
    [switch]$Agent,
    [switch]$Uninstall,
    [switch]$NoAutostart,
    [switch]$NoLaunch,
    [string]$Token = $null,
    [int]$Port = 11480,
    [string]$Branch = "main",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "LocalAIOverlayPro")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
# `irm ... | iex` can't pass switches, so environment variables work too.
if ($env:LAIO_AGENT -eq "1") { $Agent = $true }
if ($env:LAIO_UNINSTALL -eq "1") { $Uninstall = $true }

$Repo = "nomad9021/Local-AI-Overlay-Pro"
$AppName = "Local AI Overlay Pro"
$AppDir = Join-Path $InstallDir "app"
$VenvDir = Join-Path $InstallDir "venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$VenvPyw = Join-Path $VenvDir "Scripts\pythonw.exe"
$AgentConfig = Join-Path $InstallDir "agent.json"
$TaskName = "LocalAIOverlayProAgent"
$FirewallRule = "Local AI Overlay Pro Agent"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) $AppName

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "xx  $msg" -ForegroundColor Red; throw $msg }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-Installed {
    # Anything running out of our install dir: overlay and/or agent.
    Get-CimInstance Win32_Process -Filter "name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$InstallDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

function Find-Python {
    # Returns the full path of a real Python >= 3.9, or $null. The Microsoft
    # Store "python.exe" alias is a stub that exits non-zero, so it's skipped.
    $probe = "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)"
    $candidates = @(
        @("py", "-3"), @("python"), @("python3"),
        @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")),
        @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))
    )
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @()
        if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue) -and -not (Test-Path $exe)) { continue }
        try {
            $out = & $exe @rest -c $probe 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $lines = @($out)
        $ver = [version]$lines[0]
        if ($ver -ge [version]"3.9") { return $lines[1].Trim() }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "Python 3.9+ not found and winget isn't available. Install Python from https://www.python.org/downloads/ and re-run."
    }
    Say "Installing Python 3.12 with winget (per-user)..."
    winget install -e --id Python.Python.3.12 --scope user --silent `
        --accept-package-agreements --accept-source-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function New-Shortcut($path, $target, $arguments, $workdir, $description) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $workdir
    $lnk.Description = $description
    $lnk.Save()
}

function Get-Source {
    # Running from a checkout (.\install\install.ps1)? Use it instead of downloading.
    if ($PSScriptRoot) {
        $local = Split-Path $PSScriptRoot -Parent
        if (Test-Path (Join-Path $local "app.py")) {
            Say "Using local source at $local"
            return $local
        }
    }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("laio-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp "src.zip"
    $url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
    Say "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $dir = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
    if (-not $dir -or -not (Test-Path (Join-Path $dir.FullName "app.py"))) { Die "Downloaded archive doesn't look right." }
    return $dir.FullName
}

function Get-LanAddress {
    try {
        $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
                 Sort-Object RouteMetric | Select-Object -First 1
        $ip = Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
              Select-Object -First 1
        return $ip.IPAddress
    } catch { return $env:COMPUTERNAME }
}

# --------------------------------------------------------------------------- #
if ($Uninstall) {
    Say "Uninstalling $AppName"
    Stop-Installed
    if (Test-Path $VenvPy) {
        & $VenvPy (Join-Path $AppDir "app.py") --autostart off 2>$null | Out-Null
        & $VenvPy (Join-Path $AppDir "agent.py") --config $AgentConfig --uninstall-service 2>$null | Out-Null
    }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if (Test-Admin) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
        else { Warn "Startup task '$TaskName' exists; remove it from an elevated prompt." }
    }
    if ((Test-Admin) -and (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue)) {
        Remove-NetFirewallRule -DisplayName $FirewallRule
    }
    if (Test-Path $StartMenu) { Remove-Item $StartMenu -Recurse -Force }
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
    Say "Removed. Your settings are kept in $env:APPDATA\LocalAIOverlayPro (delete it to reset)."
    return
}

Say "Installing $AppName ($(if ($Agent) { 'agent' } else { 'overlay' })) to $InstallDir"

$python = Find-Python
if (-not $python) {
    Install-Python
    $python = Find-Python
    if (-not $python) { Die "Python still not found after installing. Open a new PowerShell window and re-run." }
}
Say "Using Python: $python"

Stop-Installed
$src = Get-Source
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
New-Item -ItemType Directory -Path $AppDir | Out-Null
Get-ChildItem -Path $src -Force | Where-Object { $_.Name -notin @(".git", ".venv", "venv", "__pycache__") } |
    Copy-Item -Destination $AppDir -Recurse -Force

if (-not (Test-Path $VenvPy)) {
    Say "Creating virtual environment"
    & $python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Die "Couldn't create a venv with $python" }
}
Say "Installing Python packages (PySide6, requests, psutil, nvidia-ml-py)..."
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install --disable-pip-version-check --progress-bar off -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "pip install failed (see the output above)." }

if (-not $Agent) {
    New-Item -ItemType Directory -Path $StartMenu -Force | Out-Null
    New-Shortcut (Join-Path $StartMenu "$AppName.lnk") $VenvPyw "`"$AppDir\app.py`"" $AppDir "Ollama HUD"
    New-Shortcut (Join-Path $StartMenu "$AppName Settings.lnk") $VenvPyw "`"$AppDir\app.py`" --settings" $AppDir "Settings & dashboard"
    New-Shortcut (Join-Path $StartMenu "Uninstall $AppName.lnk") "powershell.exe" `
        "-NoProfile -ExecutionPolicy Bypass -Command `"& ([scriptblock]::Create((irm https://raw.githubusercontent.com/$Repo/$Branch/install/install.ps1))) -Uninstall`"" `
        $env:USERPROFILE "Remove $AppName"
    Say "Start Menu shortcuts created"

    if (-not $NoAutostart) {
        & $VenvPy (Join-Path $AppDir "app.py") --autostart on | Out-Host
    }
    if (-not $NoLaunch) {
        Start-Process -FilePath $VenvPyw -ArgumentList "`"$AppDir\app.py`" --settings" -WorkingDirectory $AppDir
    }
    Say "Done. The overlay appears top-right when Ollama has models loaded; settings & dashboard are open now."
    Say "Tray icon: under the ^ chevron in the taskbar. Add other machines on the Hosts tab."
    return
}

# ---- agent ---------------------------------------------------------------- #
$initArgs = @((Join-Path $AppDir "agent.py"), "--config", $AgentConfig, "--init-config", "--port", $Port)
if ($PSBoundParameters.ContainsKey("Token")) { $initArgs += @("--token", $Token) }
$cfgOut = & $VenvPy @initArgs
$cfgOut | Out-Host
$agentToken = (Get-Content $AgentConfig -Raw | ConvertFrom-Json).token

if (Test-Admin) {
    Say "Registering startup task '$TaskName' (runs at boot, before login)"
    $action = New-ScheduledTaskAction -Execute $VenvPyw `
        -Argument "`"$AppDir\agent.py`" --config `"$AgentConfig`"" -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    if (-not (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Protocol TCP `
            -LocalPort $Port -Action Allow -Profile Private, Domain | Out-Null
        Say "Firewall: allowed inbound TCP $Port on Private/Domain networks"
    }
} else {
    Warn "Not elevated: the agent will start when you log in (re-run as Administrator to run it at boot and open the firewall)."
    if (-not $NoAutostart) {
        & $VenvPy (Join-Path $AppDir "agent.py") --config $AgentConfig --install-service | Out-Host
    }
    Start-Process -FilePath $VenvPyw -ArgumentList "`"$AppDir\agent.py`" --config `"$AgentConfig`"" -WorkingDirectory $AppDir
    Warn "If Windows Firewall asks about pythonw.exe, allow it on Private networks."
}

$addr = Get-LanAddress
Write-Host ""
Say "Agent installed. On the machine running the overlay: Settings -> Hosts -> Add (or Discover)"
Write-Host "      Address: $($addr):$Port"
Write-Host "      Mode:    Agent"
Write-Host "      Token:   $agentToken"
Write-Host ""
