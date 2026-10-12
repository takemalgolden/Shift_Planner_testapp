@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer is required on this server PC for this source-code prototype.
  echo The client PCs do not need Python; they use a browser.
  pause
  exit /b 1
)
py -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
set SHIFT_HOST=127.0.0.1
set SHIFT_PORT=5050
python app.py
pause
