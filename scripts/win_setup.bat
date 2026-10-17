@echo off
REM ======================================================================
REM GoldFX Auto-Trade Agent — Windows Setup
REM Run this from the goldfx-agent project folder (where .venv would live).
REM
REM What it does:
REM   1. Creates a Windows Python venv in .venv\
REM   2. Installs all deps from requirements.txt
REM   3. Installs MetaTrader5 lib (Windows-only, needed for LIVE orders)
REM   4. Reminds you to fill in .env
REM ======================================================================
setlocal
cd /d "%~dp0"

REM --- Python: try py launcher, then python, then python3 ---
where py >nul 2>nul && (set "PYTHON=py -3") || (
    where python >nul 2>nul && (set "PYTHON=python") || (
        where python3 >nul 2>nul && (set "PYTHON=python3") || (
            echo !! Python3 not found. Install from https://www.python.org/downloads/
            echo    Tick "Add to PATH" during install.
            exit /b 1
        )
    )
)

REM --- venv ---
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating venv...
    %PYTHON% -m venv .venv
) else (
    echo [1/4] .venv already exists
)

REM --- deps ---
echo [2/4] Installing requirements.txt...
call .venv\Scripts\pip install --upgrade pip wheel setuptools >nul
call .venv\Scripts\pip install -r requirements.txt
echo [3/4] Installing MetaTrader5 (Windows-only)...
call .venv\Scripts\pip install MetaTrader5 || (
    echo !! MetaTrader5 install failed.
    echo    Ensure you are on Windows 64-bit and Python is 64-bit.
)

REM --- .env ---
if not exist ".env" (
    copy .env.example .env >nul
    echo [4/4] Created .env from example.
    echo        OPEN .env in a text editor and fill in:
    echo          AUTO_TRADE_ENV=mt5
    echo          MT5_LOGIN       = ^<your Headway demo login^>
    echo          MT5_PASSWORD    = ^<your Headway demo password^>
    echo          MT5_SERVER      = ^<Headway demo server name^>
    echo          BOT_TOKEN       = ^<your Telegram bot token^>
    echo          CHAT_ID         = ^<your channel id^>
) else (
    echo [4/4] .env already exists
)

echo.
echo ============================================================
echo  Setup complete. Next steps:
echo
echo  1. Edit .env with your Headway demo credentials + Telegram.
echo  2. Log into Headway MT5 terminal (keep it open).
echo  3. Start the agent:   .venv\Scripts\python.exe scripts\run_agent.py
echo ============================================================