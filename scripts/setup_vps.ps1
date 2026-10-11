# ==============================================================================
# GoldFX Autonomous Trading Agent - Automated Windows VPS Setup Script
# Run this script in an Administrator PowerShell window on your new Windows VPS.
# ==============================================================================

#Requires -RunAsAdministrator

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "   GOLDFX AUTONOMOUS AGENT - CLOUD VPS SETUP AUTOMATION" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host ""

$ErrorActionPreference = "Stop"
$WorkingDir = "C:\goldfx-agent"

# ------------------------------------------------------------------------------
# 1. Configure High Performance & Prevent Power Sleep
# ------------------------------------------------------------------------------
Write-Host "[1/7] Optimizing Windows Power Plan for 24/7 Execution..." -ForegroundColor Yellow
try {
    powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c 2>$null # High Performance
    powercfg -change -standby-timeout-ac 0 2>$null
    powercfg -change -monitor-timeout-ac 0 2>$null
    Write-Host "      Power plan set to High Performance (Sleep Disabled)." -ForegroundColor Green
} catch {
    Write-Host "      Could not adjust power settings (skipping)." -ForegroundColor Gray
}

# ------------------------------------------------------------------------------
# 2. Check / Install Git
# ------------------------------------------------------------------------------
Write-Host "[2/7] Checking Git installation..." -ForegroundColor Yellow
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "      Git not found. Downloading Git for Windows..." -ForegroundColor White
    $gitInstaller = "$env:TEMP\Git-Installer.exe"
    $gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.44.0.windows.1/Git-2.44.0-64-bit.exe"
    Invoke-WebRequest -Uri $gitUrl -OutFile $gitInstaller -UseBasicParsing
    Write-Host "      Installing Git silently..." -ForegroundColor White
    Start-Process -FilePath $gitInstaller -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS" -Wait
    Remove-Item $gitInstaller -Force -ErrorAction SilentlyContinue
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-Host "      Git installed successfully." -ForegroundColor Green
} else {
    Write-Host "      Git is already installed." -ForegroundColor Green
}

# ------------------------------------------------------------------------------
# 3. Check / Install Python 3.11 64-bit
# ------------------------------------------------------------------------------
Write-Host "[3/7] Checking Python 64-bit installation..." -ForegroundColor Yellow
$pyInstalled = $false
try {
    $pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor} {sys.maxsize > 2**32}')" 2>$null
    if ($pyVer -and ($pyVer -match "3\.(10|11|12)") -and ($pyVer -match "True")) {
        $pyInstalled = $true
        Write-Host "      Compatible Python found: $pyVer" -ForegroundColor Green
    }
} catch {}

if (-not $pyInstalled) {
    Write-Host "      Downloading Python 3.11.9 (64-bit)..." -ForegroundColor White
    $pyInstaller = "$env:TEMP\python-3.11.9-amd64.exe"
    $pyUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller -UseBasicParsing
    Write-Host "      Installing Python (Add to PATH enabled)..." -ForegroundColor White
    Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
    Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    Write-Host "      Python 3.11 (64-bit) installed successfully." -ForegroundColor Green
}

# ------------------------------------------------------------------------------
# 4. Clone or Update Repository
# ------------------------------------------------------------------------------
Write-Host "[4/7] Setting up GoldFX repository at $WorkingDir..." -ForegroundColor Yellow
if (-not (Test-Path $WorkingDir)) {
    Write-Host "      Cloning repository from GitHub..." -ForegroundColor White
    & git clone https://github.com/Glitch-WRLD/goldfx-github.git $WorkingDir
    Write-Host "      Cloned successfully." -ForegroundColor Green
} else {
    Write-Host "      Repository directory exists. Pulling latest code..." -ForegroundColor White
    Set-Location $WorkingDir
    & git pull origin main
    Write-Host "      Updated to latest commit." -ForegroundColor Green
}

Set-Location $WorkingDir

# ------------------------------------------------------------------------------
# 5. Create Virtual Environment & Install Dependencies
# ------------------------------------------------------------------------------
Write-Host "[5/7] Setting up Python virtual environment & dependencies..." -ForegroundColor Yellow
if (-not (Test-Path "$WorkingDir\.venv")) {
    Write-Host "      Creating .venv..." -ForegroundColor White
    & python -m venv "$WorkingDir\.venv"
}

$venvPip = "$WorkingDir\.venv\Scripts\pip.exe"
$venvPy = "$WorkingDir\.venv\Scripts\python.exe"

Write-Host "      Upgrading pip and installing packages..." -ForegroundColor White
& $venvPip install --upgrade pip wheel setuptools
& $venvPip install -r "$WorkingDir\requirements.txt"
Write-Host "      All dependencies installed successfully." -ForegroundColor Green

# ------------------------------------------------------------------------------
# 6. Download Headway MT5 Installer to Desktop
# ------------------------------------------------------------------------------
Write-Host "[6/7] Preparing Headway MT5 Installer..." -ForegroundColor Yellow
$desktop = [Environment]::GetFolderPath("Desktop")
$mt5InstallerPath = "$desktop\Headway5Setup.exe"
if (-not (Test-Path $mt5InstallerPath)) {
    try {
        Write-Host "      Downloading Headway MT5 installer to Desktop..." -ForegroundColor White
        $mt5Url = "https://download.mql5.com/cdn/web/headway.services.ltd/mt5/headway5setup.exe"
        Invoke-WebRequest -Uri $mt5Url -OutFile $mt5InstallerPath -UseBasicParsing
        Write-Host "      Saved to Desktop: Headway5Setup.exe" -ForegroundColor Green
    } catch {
        Write-Host "      Could not auto-download MT5 installer (download directly from headway2.com)." -ForegroundColor Gray
    }
} else {
    Write-Host "      Headway MT5 installer already present on Desktop." -ForegroundColor Green
}

# ------------------------------------------------------------------------------
# 7. Configure 24/7 Windows Task Scheduler Watchdog
# ------------------------------------------------------------------------------
Write-Host "[7/7] Configuring 24/7 Watchdog in Windows Task Scheduler..." -ForegroundColor Yellow
$taskName = "GoldFX_Autonomous_Agent"
$batchPath = "$WorkingDir\start_agent.bat"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Create Task: Run on system boot, highest privileges, auto-restart on crash
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batchPath`"" -WorkingDirectory $WorkingDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User "NT AUTHORITY\SYSTEM" -RunLevel Highest | Out-Null
Write-Host "      Scheduled Task '$taskName' created with SYSTEM privileges (Starts on boot, runs 24/7)." -ForegroundColor Green

Write-Host ""
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "   VPS SETUP COMPLETE! FINAL STEPS TO GO LIVE:" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "1. MT5 Terminal Setup:" -ForegroundColor White
Write-Host "   - Double-click 'Headway5Setup.exe' on Desktop to install MT5."
Write-Host "   - Log into your Cent account (Login: 17715937, Server: Headway-Real)."
Write-Host "   - In MT5: Tools -> Options -> Expert Advisors -> CHECK 'Allow Algo Trading'."
Write-Host ""
Write-Host "2. Copy .env File:" -ForegroundColor White
Write-Host "   - Copy your current '.env' file into '$WorkingDir\.env'."
Write-Host ""
Write-Host "3. Start the Autonomous System:" -ForegroundColor White
Write-Host "   - Open PowerShell and run:"
Write-Host "     Start-ScheduledTask -TaskName '$taskName'"
Write-Host "   - Or test interactively in console:"
Write-Host "     C:\goldfx-agent\.venv\Scripts\python.exe scripts\run_agent.py"
Write-Host "=================================================================" -ForegroundColor Cyan
