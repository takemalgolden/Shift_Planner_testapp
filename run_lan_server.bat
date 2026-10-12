@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run run_local.bat once to install the server runtime.
  pause
  exit /b 1
)
call .venv\Scripts\activate
set SHIFT_HOST=0.0.0.0
set SHIFT_PORT=5050
python app.py
pause
