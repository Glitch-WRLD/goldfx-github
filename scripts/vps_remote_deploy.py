import sys
import os
import time
from pathlib import Path
import paramiko

VPS_IP = os.getenv("VPS_IP", "169.58.146.95")
VPS_USER = os.getenv("VPS_USER", "administrator")
VPS_PASS = os.getenv("VPS_PASS")
LOCAL_DIR = Path("C:/goldfx-agent")

if not VPS_PASS:
    cfg_file = LOCAL_DIR / "vps_config.json"
    if cfg_file.exists():
        import json
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        VPS_PASS = cfg.get("password", "")

def run_ssh_command(client, cmd: str, timeout=600):
    print(f"\n[EXEC] {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    
    # Stream stdout
    while True:
        line = stdout.readline()
        if not line:
            break
        print(f"  {line.rstrip()}")
    
    exit_code = stdout.channel.recv_exit_status()
    err = stderr.read().decode().strip()
    if err and exit_code != 0:
        print(f"[ERROR (code {exit_code})]: {err}")
    return exit_code

def main():
    print("=" * 70)
    print("GOLDFX CLOUD VPS AUTOMATED REMOTE DEPLOYMENT")
    print(f"Target: {VPS_USER}@{VPS_IP}")
    print("=" * 70)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    print("\nConnecting to VPS via SSH...")
    client.connect(VPS_IP, port=22, username=VPS_USER, password=VPS_PASS, timeout=15)
    print("Connected successfully!")

    # 1. Optimize Windows Power Plan
    print("\n--- 1. Setting High Performance Power Plan ---")
    run_ssh_command(client, 'powershell -Command "powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c 2>$null; powercfg -change -standby-timeout-ac 0 2>$null; powercfg -change -monitor-timeout-ac 0 2>$null; Write-Host \'Power plan configured.\'"')

    # 2. Check / Install Git
    print("\n--- 2. Checking / Installing Git ---")
    ps_git = '''powershell -Command "
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host 'Downloading Git 64-bit...';
        $installer = '$env:TEMP\\git_install.exe';
        Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.44.0.windows.1/Git-2.44.0-64-bit.exe' -OutFile $installer -UseBasicParsing;
        Write-Host 'Installing Git silently...';
        Start-Process $installer -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS' -Wait;
        Remove-Item $installer -Force -ErrorAction SilentlyContinue;
        Write-Host 'Git installed.';
    } else {
        Write-Host 'Git already installed:' (git --version);
    }
    "'''
    run_ssh_command(client, ps_git)

    # 3. Check / Install Python 3.11 64-bit
    print("\n--- 3. Checking / Installing Python 3.11 (64-bit) ---")
    ps_py = '''powershell -Command "
    $py = Get-Command python -ErrorAction SilentlyContinue;
    $needPy = $true;
    if ($py) {
        $ver = python -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor} {sys.maxsize > 2**32}\")' 2>$null;
        if ($ver -match '3\.(10|11|12)' -and $ver -match 'True') {
            Write-Host 'Compatible 64-bit Python found:' $ver;
            $needPy = $false;
        }
    }
    if ($needPy) {
        Write-Host 'Downloading Python 3.11.9 (64-bit)...';
        $installer = '$env:TEMP\\py_install.exe';
        Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile $installer -UseBasicParsing;
        Write-Host 'Installing Python with PATH...';
        Start-Process $installer -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait;
        Remove-Item $installer -Force -ErrorAction SilentlyContinue;
        Write-Host 'Python installed.';
    }
    "'''
    run_ssh_command(client, ps_py)

    # 4. Clone or Pull Repository
    print("\n--- 4. Setting Up C:\\goldfx-agent Repository ---")
    ps_repo = '''powershell -Command "
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User');
    if (-not (Test-Path 'C:\\goldfx-agent')) {
        Write-Host 'Cloning repository...';
        git clone https://github.com/Glitch-WRLD/goldfx-github.git C:\\goldfx-agent;
    } else {
        Write-Host 'Pulling latest code...';
        cd C:\\goldfx-agent;
        git pull origin main;
    }
    Write-Host 'Repository ready at C:\\goldfx-agent.';
    "'''
    run_ssh_command(client, ps_repo)

    # 5. Securely Transfer Local .env via SFTP
    print("\n--- 5. Uploading .env Configuration via SFTP ---")
    sftp = client.open_sftp()
    local_env = LOCAL_DIR / ".env"
    remote_env = "C:/goldfx-agent/.env"
    if local_env.exists():
        print(f"Uploading {local_env} -> {remote_env}...")
        sftp.put(str(local_env), remote_env)
        print(".env uploaded successfully!")
    else:
        print("[WARNING] Local .env not found!")
    sftp.close()

    # 6. Create Virtual Environment & Install Dependencies
    print("\n--- 6. Creating .venv & Installing Dependencies ---")
    ps_venv = '''powershell -Command "
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User');
    cd C:\\goldfx-agent;
    if (-not (Test-Path 'C:\\goldfx-agent\\.venv')) {
        Write-Host 'Creating .venv...';
        python -m venv .venv;
    }
    Write-Host 'Installing dependencies from requirements.txt...';
    .\\.venv\\Scripts\\pip install --upgrade pip wheel setuptools;
    .\\.venv\\Scripts\\pip install -r requirements.txt;
    Write-Host 'Venv dependencies installed.';
    "'''
    run_ssh_command(client, ps_venv, timeout=900)

    # 7. Download Headway MT5 Installer to Desktop
    print("\n--- 7. Preparing Headway MT5 Installer on Desktop ---")
    ps_mt5_dl = '''powershell -Command "
    $desktop = 'C:\\Users\\Administrator\\Desktop';
    $mt5Installer = \"$desktop\\Headway5Setup.exe\";
    if (-not (Test-Path $mt5Installer)) {
        Write-Host 'Downloading Headway MT5 installer to Desktop...';
        try {
            Invoke-WebRequest -Uri 'https://download.mql5.com/cdn/web/headway.services.ltd/mt5/headway5setup.exe' -OutFile $mt5Installer -UseBasicParsing;
            Write-Host 'Downloaded Headway5Setup.exe to Desktop.';
        } catch {
            Write-Host 'Failed to auto-download MT5 installer:' $_;
        }
    } else {
        Write-Host 'Headway5Setup.exe already exists on Desktop.';
    }
    "'''
    run_ssh_command(client, ps_mt5_dl)

    # 8. Check MT5 Terminal Installation Status
    print("\n--- 8. Checking MT5 Installation Status ---")
    ps_check_mt5 = '''powershell -Command "
    $mt5Paths = @(
        'C:\\Program Files\\Headway MT5\\terminal64.exe',
        'C:\\Program Files\\MetaTrader 5\\terminal64.exe'
    );
    $found = $false;
    foreach ($p in $mt5Paths) {
        if (Test-Path $p) {
            Write-Host \"[FOUND] MT5 Terminal installed at: $p\";
            $found = $true;
            break;
        }
    }
    if (-not $found) {
        Write-Host '[ACTION REQUIRED] Headway MT5 terminal not installed yet. Double-click Headway5Setup.exe on Desktop to install!';
    }
    "'''
    run_ssh_command(client, ps_check_mt5)

    # 9. Register Scheduled Task Watchdog
    print("\n--- 9. Registering Windows Scheduled Task (24/7 Watchdog) ---")
    ps_sched = '''powershell -Command "
    $taskName = 'GoldFX_Autonomous_Agent';
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue;
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"C:\\goldfx-agent\\start_agent.bat\"' -WorkingDirectory 'C:\\goldfx-agent';
    $trigger = New-ScheduledTaskTrigger -AtStartup;
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365);
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User 'NT AUTHORITY\\SYSTEM' -RunLevel Highest | Out-Null;
    Write-Host 'Scheduled Task GoldFX_Autonomous_Agent registered successfully!';
    "'''
    run_ssh_command(client, ps_sched)

    client.close()
    print("\n" + "=" * 70)
    print("DEPLOYMENT SCRIPT COMPLETED SUCCESSFULLY!")
    print("=" * 70)

if __name__ == "__main__":
    main()
