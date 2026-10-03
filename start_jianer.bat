@echo off
REM Run Jianer using the bundled virtual environment python.
REM This file is called by ..\jianer-start.bat (the one-click launcher).
cd /d "%~dp0"

if not exist ".\venv\Scripts\python.exe" goto err_no_venv

.\venv\Scripts\python.exe main.py
echo.
echo [bot exited] press any key to close this window.
pause
exit /b 0

:err_no_venv
echo [X] venv python not found:
echo     %~dp0venv\Scripts\python.exe
echo     Recreate the virtual environment, or fix the path in this file.
pause
exit /b 1
