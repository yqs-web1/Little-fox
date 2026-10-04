@echo off
setlocal
REM ============================================================
REM  Little Fox QQ Bot - one-click launcher
REM  Project : E:\little fox\Jianer_Next_QQ_Bot
REM  NapCat  : E:\little fox\NapCat.Shell
REM  Keep this file pure ASCII. Do NOT write Chinese characters here:
REM  cmd mis-parses a .bat that holds UTF-8 Chinese (verified).
REM ============================================================

set "BOT_DIR=E:\little fox\Jianer_Next_QQ_Bot"
set "NAPCAT_DIR=E:\little fox\NapCat.Shell"
REM LM Studio CLI. Change this line if LM Studio is installed elsewhere.
set "LMS=C:\Users\win\.lmstudio\bin\lms.exe"
REM Keep the local model loaded: unload only after this many idle seconds (24h).
set "LMS_TTL=86400"

if not exist "%BOT_DIR%\start_jianer.bat" goto err_no_bot

REM ---- Read backend / local model / API-key status from config.json ----
REM read_backend.py prints one line:  <backend>|<local_model>|<key_ok>
REM Run it via RELATIVE paths from inside the project dir: a quoted absolute
REM path with a space ("little fox") gets its quotes stripped by cmd /c inside
REM for /f, splitting the command at the space.
set "AI_BACKEND=local"
set "LOCAL_MODEL=google/gemma-3-4b"
set "KEY_OK=1"
if not exist "%BOT_DIR%\read_backend.py" goto cfg_ready
pushd "%BOT_DIR%"
for /f "tokens=1,2,3 delims=|" %%a in ('venv\Scripts\python.exe read_backend.py') do (
    set "AI_BACKEND=%%a"
    set "LOCAL_MODEL=%%b"
    set "KEY_OK=%%c"
)
popd
:cfg_ready

echo Project : %BOT_DIR%
echo Backend : %AI_BACKEND%
echo Model   : %LOCAL_MODEL%
echo.

REM ---- Warn early if cloud mode has no key (every AI reply would fail) ----
if /i not "%AI_BACKEND%"=="cloud" goto skip_key_warn
if not "%KEY_OK%"=="0" goto skip_key_warn
echo [WARN] ai_backend=cloud but no API key was found.
echo        Set the env var JIANER_API_KEY (or DEEPSEEK_API_KEY), or fill
echo        Others.deepseek_key in config.json, then run this launcher again.
echo        The bot will still start, but every AI reply will fail.
echo.
:skip_key_warn

echo [1/4] Checking NapCat (QQ WebSocket port 5004)...
call :port_open 5004
if not errorlevel 1 goto napcat_running

echo       NapCat NOT detected.
if not exist "%NAPCAT_DIR%\NapCatWinBootMain.exe" goto err_no_napcat
echo       WARNING: this will force-close ALL QQ.exe processes on this PC
echo                (taskkill /f /im QQ.exe), including a personal QQ session.
echo                Press Ctrl+C within 5 seconds to cancel.
timeout /t 5 /nobreak
if exist "%NAPCAT_DIR%\KillQQ.bat" call "%NAPCAT_DIR%\KillQQ.bat"
timeout /t 3 /nobreak >nul
start "NapCat" /D "%NAPCAT_DIR%" cmd /c launcher-user.bat
echo       Waiting for NapCat to open port 5004 (up to 60s)...
set /a WAIT_TRIES=0
:wait_napcat
call :port_open 5004
if not errorlevel 1 goto napcat_ready
set /a WAIT_TRIES+=1
if %WAIT_TRIES% geq 20 goto napcat_timeout
timeout /t 3 /nobreak >nul
goto wait_napcat
:napcat_timeout
echo       [WARN] port 5004 did not open within 60s.
echo              If QQ is asking for a QR-code login, finish it in the QQ window;
echo              the bot keeps retrying in the background, so just scan and wait.
goto napcat_after
:napcat_ready
echo       NapCat is ready.
goto napcat_after
:napcat_running
echo       NapCat is running.
:napcat_after

REM ---- LM Studio is only needed for the local backend ----
if /i "%AI_BACKEND%"=="cloud" goto lms_skip
if not exist "%LMS%" goto err_no_lms
echo [2/4] Starting LM Studio server (local AI)...
"%LMS%" server start
timeout /t 3 /nobreak >nul
echo [3/4] Loading local model %LOCAL_MODEL% ...
"%LMS%" load --ttl %LMS_TTL% -y "%LOCAL_MODEL%"
timeout /t 2 /nobreak >nul
goto lms_done
:lms_skip
echo [2/4] Cloud backend -- skipping LM Studio.
echo [3/4] No local model needed.
:lms_done

echo [4/4] Starting bot...
echo.
cd /d "%BOT_DIR%"
call "%BOT_DIR%\start_jianer.bat"
exit /b 0

REM ================= error branches =================
:err_no_bot
echo [X] bot project not found: %BOT_DIR%
echo     Expected start_jianer.bat there.
pause
exit /b 1

:err_no_napcat
echo [X] NapCat not found at %NAPCAT_DIR%
echo     Expected NapCatWinBootMain.exe there. Fix NAPCAT_DIR at the top of this file.
pause
exit /b 1

:err_no_lms
echo [X] LM Studio CLI not found: %LMS%
echo     Fix the LMS path at the top of this file, start LM Studio manually,
echo     or switch the backend to cloud: set Others.ai_backend to "cloud"
echo     in config.json (or send the cloud command to the bot in QQ).
pause
exit /b 1

REM ================= helpers =================
REM errorlevel 0 when something is LISTENING on port %1
:port_open
netstat -ano | findstr ":%1" | findstr "LISTENING" >nul 2>&1
exit /b %errorlevel%
