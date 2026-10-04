@echo off
rem ===================================================================
rem  Little Fox Console launcher
rem  Keep this file pure ASCII: cmd.exe mis-parses UTF-8 Chinese here.
rem ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "BOTDIR=%~dp0..\Jianer_Next_QQ_Bot"
set "PY=%BOTDIR%\venv\Scripts\python.exe"

if not exist "%BOTDIR%\config.json" (
  echo [ERROR] config.json not found under:
  echo         %BOTDIR%
  echo         Use --bot-dir to point at the right folder.
  pause
  exit /b 1
)

if not exist "%PY%" (
  echo [WARN] venv python not found, falling back to "python" on PATH.
  echo        Expected: %PY%
  set "PY=python"
)

"%PY%" -c "import flask" 2>nul
if errorlevel 1 (
  echo [ERROR] Flask is not importable by: %PY%
  echo         Install it:  "%PY%" -m pip install flask
  pause
  exit /b 1
)

echo ================================================================
echo   Little Fox Console
echo   bot dir : %BOTDIR%
echo   python  : %PY%
echo ================================================================
echo.
"%PY%" server.py --bot-dir "%BOTDIR%" %*

echo.
echo [console stopped]
pause
