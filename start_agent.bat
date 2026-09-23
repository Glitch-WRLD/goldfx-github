@echo off
cd /d C:\goldfx-agent
echo [%date% %time%] start_agent.bat launched from desktop session
.venv\Scripts\python.exe scripts\run_agent.py
echo [%date% %time%] agent exited (code %errorlevel%)
pause
