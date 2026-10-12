@echo off
setlocal
cd /d "%~dp0"
set "SERVER_NAME=%COMPUTERNAME%"
set "OUT=%~dp0シフト管理.url"
(
  echo [InternetShortcut]
  echo URL=http://%SERVER_NAME%:5050/
) > "%OUT%"
echo.
echo Created: %OUT%
echo Server computer name: %SERVER_NAME%
echo.
echo Copy the file "シフト管理.url" to employee PCs or an internal shared folder.
echo Employees can double-click it; they do not need to type an IP address.
echo.
echo If it does not open from another PC, the company LAN may not resolve computer names.
echo Ask IT to configure an internal DNS name/alias for this server.
pause
