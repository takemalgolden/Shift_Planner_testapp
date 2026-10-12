@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo Shift Planner - Windows EXE build
echo ==========================================
echo.
echo This build must be run on a Windows 10/11 PC.
echo Internet is required for this one-time build.
echo.
pause

set "PYDIR=%~dp0.build_python"
set "PYZIP=%~dp0.build_python.zip"
set "PYURL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"

if not exist "%PYDIR%\python.exe" (
  echo Downloading portable Python runtime...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYZIP%'"
  if errorlevel 1 goto :error
  if not exist "%PYDIR%" mkdir "%PYDIR%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force '%PYZIP%' '%PYDIR%'"
  if errorlevel 1 goto :error
)

if not exist "%PYDIR%\python.exe" (
  echo ERROR: python.exe was not found in %PYDIR%
  goto :error
)

echo Enabling site-packages for the embedded Python runtime...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pth=Join-Path '%PYDIR%' 'python312._pth'; if (!(Test-Path $pth)) { Write-Error 'python312._pth not found'; exit 1 }; $c=Get-Content $pth; $c=$c -replace '^\s*#\s*import site\s*$','import site'; if (!($c -match '^\s*import site\s*$')) { $c += 'import site' }; Set-Content -Path $pth -Value $c -Encoding ascii"
if errorlevel 1 goto :error

echo Preparing pip in the portable runtime...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%PYDIR%\get-pip.py'"
if errorlevel 1 goto :error

"%PYDIR%\python.exe" "%PYDIR%\get-pip.py"
if errorlevel 1 goto :error

echo Verifying pip...
"%PYDIR%\python.exe" -m pip --version
if errorlevel 1 goto :error

echo Installing build dependencies...
"%PYDIR%\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%PYDIR%\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo Building EXE...
"%PYDIR%\python.exe" -m PyInstaller --noconfirm --clean --onedir --name ShiftPlannerServer ^
  --add-data "templates;templates" app.py
if errorlevel 1 goto :error

echo.
echo Build complete.
echo EXE location: dist\ShiftPlannerServer\ShiftPlannerServer.exe
echo Keep the entire dist\ShiftPlannerServer folder together.
echo Copy that folder to the always-on administrator PC.
echo.
echo NOTE: This is a prototype package, not production-approved software.
pause
exit /b 0

:error
echo.
echo BUILD FAILED. Review the error above.
pause
exit /b 1
