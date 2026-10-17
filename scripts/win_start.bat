@echo off
REM ======================================================================
REM GoldFX Auto-Trade Agent — Windows start (foreground, logs to console)
REM Ensure Headway MT5 terminal is RUNNING and logged in first.
REM To run in background later, use the Task Scheduler step in README or
REM wrap as:  start /min .venv\Scripts\pythonw.exe scripts\run_agent.py
REM ======================================================================
setlocal
cd /d "%~dp0.."

if not exist ".env" (
    echo !! .env not found. Run win_setup.bat first.
    exit /b 1
)

echo Starting GoldFX agent (AUTO_TRADE_ENV from .env)...
call .venv\Scripts\python.exe scripts\run_agent.py