@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto needs_setup
if not exist "bridge.json" goto needs_setup

".venv\Scripts\python.exe" bridge.py
echo.
echo The bridge has stopped. Review the message above if it did not start.
pause
exit /b %errorlevel%

:needs_setup
echo Run setup_windows.bat first.
pause
exit /b 1
