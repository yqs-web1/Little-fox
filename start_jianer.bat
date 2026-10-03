@echo off
cd /d "%~dp0"
REM Run Jianer using the bundled virtual environment python
.\venv\Scripts\python.exe main.py
pause
