@echo off
rem Removes the PCS 1-Stop worker's scheduled task (see uninstall.ps1).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
set RC=%errorlevel%
echo.
rem started by a double click: keep the window open so the messages can be read (set PCS_NOPAUSE=1 to never wait)
if not defined PCS_NOPAUSE echo %cmdcmdline% | find /i "%~f0" >nul && pause
exit /b %RC%
