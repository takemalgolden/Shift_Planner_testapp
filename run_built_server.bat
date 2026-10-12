@echo off
setlocal
cd /d "%~dp0"
if exist "dist\ShiftPlannerServer\ShiftPlannerServer.exe" (
  start "" "%~dp0dist\ShiftPlannerServer\ShiftPlannerServer.exe"
) else (
  echo EXE not found.
  echo First run build_windows_exe.bat on Windows 10/11.
  pause
)
