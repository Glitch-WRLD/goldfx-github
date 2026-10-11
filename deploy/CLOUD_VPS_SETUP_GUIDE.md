# GoldFX Cloud VPS Deployment & Migration Guide

This guide walks you through migrating the **GoldFX Autonomous Trading System** from your local computer to a dedicated 24/7 Windows Cloud VPS (Virtual Private Server).

---

## 1. Top VPS Provider Recommendations

Because the MetaTrader 5 Python bridge requires native Windows 64-bit API access, you **must use a Windows VPS** (Windows Server 2022).

| Provider | Recommended Plan | Specs | Approx Cost | Best For | Location |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ForexVPS.net** *(Recommended for Trading)* | **Basic+ or Standard** | 2-4 vCPU, 4-8 GB RAM, 60GB NVMe | ~$25 - $35/mo | Pre-configured for MT5, optimized low-latency trading kernel, 100% uptime SLA | **London (LD4)** |
| **Contabo** *(Best Value / High Power)* | **Cloud VPS S or M** + Windows Server 2022 | 4-6 vCPU, 8-16 GB RAM, 100-200GB SSD | ~$12 - $17/mo | Huge memory & compute for low monthly price | **United Kingdom (London)** or Germany |
| **Kamatera** *(Best for Free Trial)* | **Custom Server** + Windows Server 2022 | 2-4 vCPU, 4-8 GB RAM, 50GB SSD | **30-Day Free Trial** ($100 credit) | Testing cloud setup before committing to a paid plan | **London** or Frankfurt |

> [!TIP]
> **Data Center Location:** Always select **London (UK)** or **Frankfurt (Germany)**. Headway's primary trading servers are hosted in Europe; choosing London or Frankfurt cuts trade execution latency to sub-5ms.

---

## 2. How to Connect to Your Windows VPS

Once you complete purchase, the VPS provider will email you:
- **IP Address** (e.g. `195.201.xxx.xxx`)
- **Username** (usually `Administrator`)
- **Password**

### From Windows:
1. Press `Windows Key + R`.
2. Type `mstsc` and press **Enter** (opens Remote Desktop Connection).
3. Enter the **IP Address**, click **Connect**.
4. Enter `Administrator` and your VPS password.

### From Mac / iOS / Android:
- Download **Microsoft Remote Desktop** from the App Store / Google Play, add a PC with the IP address, and log in.

---

## 3. Automated One-Command Installation on the VPS

Once connected to the desktop of your Windows VPS:

1. Right-click the Windows Start menu and select **PowerShell (Admin)** or **Windows Terminal (Admin)**.
2. Run this single command to download and run the automated setup script:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12; irm https://raw.githubusercontent.com/Glitch-WRLD/goldfx-github/main/scripts/setup_vps.ps1 | iex
```

### What This Automated Script Does:
- Configures Windows to **High Performance** (disables power saving, standby, and sleep).
- Silently installs **Git for Windows (64-bit)**.
- Silently installs **Python 3.11.9 (64-bit)** with PATH configured.
- Clones `goldfx-github` to `C:\goldfx-agent`.
- Builds the Python `.venv` and installs all dependencies (`MetaTrader5`, `httpx`, `pandas`, `numpy`, etc.).
- Downloads the **Headway MT5 installer** directly to the VPS desktop.
- Registers a **Windows Task Scheduler Watchdog** (`GoldFX_Autonomous_Agent`) that launches on boot and restarts automatically if ever interrupted.

---

## 4. MT5 Terminal & Account Login

1. On the VPS Desktop, double-click **`Headway5Setup.exe`** to install the Headway MT5 terminal.
2. In MT5, click **File $\rightarrow$ Login to Trade Account**:
   - **Login:** `17715937`
   - **Password:** `<Your Headway Cent Password>`
   - **Server:** `Headway-Real`
3. Click **Tools $\rightarrow$ Options $\rightarrow$ Expert Advisors**:
   - Check **"Allow Algo Trading"**.
   - Check **"Allow DLL imports"**.
   - Click **OK**.

---

## 5. Transferring Your `.env` Configuration File

1. On your local PC, open [`C:\goldfx-agent\.env`](file:///C:/goldfx-agent/.env).
2. Copy the entire file content.
3. On the VPS, create `C:\goldfx-agent\.env` (using Notepad or VS Code) and paste the content.
4. Save the file.

---

## 6. Going Live & Shutting Down Local Instance

> [!CAUTION]
> **Avoid Duplicate Orders:** Ensure you stop the agent running on your local PC before starting the cloud agent so two instances do not trade on the same account simultaneously.

1. **On your local PC:**
   - Stop the local watchdog loop or close the agent console.
2. **On the Cloud VPS:**
   - In PowerShell, start the 24/7 background agent:
     ```powershell
     Start-ScheduledTask -TaskName "GoldFX_Autonomous_Agent"
     ```
   - Or test it interactively first to inspect logs:
     ```powershell
     cd C:\goldfx-agent
     .\.venv\Scripts\python.exe scripts\run_agent.py
     ```
3. Check [`logs/agent.log`](file:///C:/goldfx-agent/logs/agent.log) on the VPS to confirm:
   - MT5 connection: `Headway-Real account 17715937 connected`
   - NTP Clock sync: `< 0.5s skew`
   - State polling: `200 OK`

Once verified, you can close Remote Desktop. The agent will run autonomously 24/7 in the cloud.
