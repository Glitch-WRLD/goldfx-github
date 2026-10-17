@echo off
cd /d C:\goldfx-agent
.venv\Scripts\python.exe C:\goldfx-agent\scripts\candle_pull.py
echo.
echo DONE - closing in 10s
timeout /t 10
