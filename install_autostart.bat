@echo off
rem goldfx agent autostart installer (one-time) - puts a launcher in the user Startup folder
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK=%STARTUP%\goldfx_agent.bat"
if not exist "%STARTUP%" mkdir "%STARTUP%"
copy /Y "C:\goldfx-agent\start_agent.bat" "%LINK%" >NUL
if exist "%LINK%" (echo STARTUP_INSTALLED "%LINK%") else (echo STARTUP_FAILED)
