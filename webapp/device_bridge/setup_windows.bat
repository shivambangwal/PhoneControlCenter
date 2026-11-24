@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto setup_failed

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto setup_failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_failed

where adb >nul 2>&1
if errorlevel 1 echo ADB was not found. Install Android SDK Platform-Tools and add platform-tools to PATH.

if exist "bridge.json" goto ready
set /p "DASHBOARD_ORIGIN=Enter your Hostinger HTTPS origin (example https://phones.example.com): "
if not defined DASHBOARD_ORIGIN goto no_origin
".venv\Scripts\python.exe" bridge.py --init --origin "%DASHBOARD_ORIGIN%"
if errorlevel 1 goto setup_failed

:ready
echo.
echo Setup complete. Keep bridge.json private; it contains the local access token.
echo Start the bridge with run_windows.bat.
pause
exit /b 0

:no_python
echo Python 3 is not installed or the py launcher is missing.
echo Install Python 3.10 or newer, then run this file again.
pause
exit /b 1

:no_origin
echo The Hostinger HTTPS origin is required to create the local bridge config.
pause
exit /b 1

:setup_failed
echo Setup failed. Read the error above, correct it, and run this file again.
pause
exit /b 1
