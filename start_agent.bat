@echo off
cd /d C:\goldfx-agent
:loop
echo [%date% %time%] start_agent.bat launched from desktop session
.venv\Scripts\python.exe scripts\run_agent.py
echo [%date% %time%] agent exited (code %errorlevel%). Restarting in 5s...
timeout /t 5 /nobreak >NUL
goto loop
