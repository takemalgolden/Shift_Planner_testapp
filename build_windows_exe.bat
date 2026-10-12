@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYDIR=%~dp0.build_python"
set "PYZIP=%~dp0.build_python.zip"
set "PYURL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "LOG=%~dp0build_log.txt"
echo Build started > "%LOG%"
echo This process needs internet for building, but not for running the EXE.
echo Details are saved to build_log.txt
echo.
if not exist "%PYDIR%\python.exe" (
 echo Downloading portable Python 3.12 x64...
 powershell -NoProfile -Command "Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYZIP%'"
 if errorlevel 1 goto :error
 if not exist "%PYDIR%" mkdir "%PYDIR%"
 powershell -NoProfile -Command "Expand-Archive -Force '%PYZIP%' '%PYDIR%'"
 if errorlevel 1 goto :error
)
if not exist "%PYDIR%\python.exe" goto :error
powershell -NoProfile -Command "$p=Join-Path '%PYDIR%' 'python312._pth'; $s=Get-Content $p; $s=$s -replace '^\s*#\s*import site\s*$','import site'; if (-not ($s -match '^\s*import site\s*$')) {$s += 'import site'}; Set-Content $p $s -Encoding ASCII"
if errorlevel 1 goto :error
"%PYDIR%\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
 echo Installing pip...
 powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%PYDIR%\get-pip.py'"
 if errorlevel 1 goto :error
 "%PYDIR%\python.exe" "%PYDIR%\get-pip.py" >> "%LOG%" 2>&1
 if errorlevel 1 goto :error
)
echo Installing Flask, openpyxl, OR-Tools and PyInstaller...
"%PYDIR%\python.exe" -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: -r requirements.txt "pyinstaller>=6,<7" >> "%LOG%" 2>&1
if errorlevel 1 goto :error
echo Checking OR-Tools import...
"%PYDIR%\python.exe" -c "from ortools.sat.python import cp_model; print('OR-Tools import OK')" >> "%LOG%" 2>&1
if errorlevel 1 (
 echo OR-Tools could not be imported. See build_log.txt.
 goto :error
)
echo Closing old ShiftPlannerServer process, if any...
taskkill /F /IM ShiftPlannerServer.exe >nul 2>&1
if exist "dist\ShiftPlannerServer" (
 rmdir /s /q "dist\ShiftPlannerServer"
 if exist "dist\ShiftPlannerServer" (
  echo Cannot remove old build. Close Explorer and running EXE.
  goto :error
 )
)
echo Building Windows EXE...
"%PYDIR%\python.exe" -m PyInstaller --noconfirm --clean --onedir --name ShiftPlannerServer --collect-all ortools --collect-all numpy --collect-all pandas --collect-all scipy --add-data "templates;templates" app.py >> "%LOG%" 2>&1
if errorlevel 1 goto :error
if not exist "dist\ShiftPlannerServer\ShiftPlannerServer.exe" goto :error
echo.
echo SUCCESS: dist\ShiftPlannerServer\ShiftPlannerServer.exe
echo Copy the entire ShiftPlannerServer directory, not only the EXE.
pause
exit /b 0
:error
echo.
echo BUILD FAILED. Please check build_log.txt in this folder.
echo Send the last 30 lines of build_log.txt for diagnosis.
pause
exit /b 1
