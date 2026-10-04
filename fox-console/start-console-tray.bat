@echo off
rem ===================================================================
rem  Little Fox Console - tray mode launcher
rem  Starts the console with no visible window (pythonw) and a tray icon.
rem  Keep this file pure ASCII: cmd.exe mis-parses UTF-8 Chinese here.
rem ===================================================================
setlocal
cd /d "%~dp0"

set "BOTDIR=%~dp0..\Jianer_Next_QQ_Bot"
set "PYW=%BOTDIR%\venv\Scripts\pythonw.exe"
set "PY=%BOTDIR%\venv\Scripts\python.exe"

if not exist "%BOTDIR%\config.json" (
  echo [ERROR] config.json not found: %BOTDIR%
  exit /b 1
)

if exist "%PYW%" (
  start "" "%PYW%" "%~dp0tray.py" --bot-dir "%BOTDIR%" %*
) else (
  if not exist "%PY%" set "PY=python"
  start "" /min "%PY%" "%~dp0tray.py" --bot-dir "%BOTDIR%" %*
)
exit /b 0
