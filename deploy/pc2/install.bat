@echo off
rem Installs the PCS 1-Stop worker as a scheduled task that starts at every logon (see install.ps1).
rem   install.bat            test the connection, install and start
rem   install.bat -DryRun    only show what would be done
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%errorlevel%
echo.
rem started by a double click: keep the window open so the messages can be read (set PCS_NOPAUSE=1 to never wait)
if not defined PCS_NOPAUSE echo %cmdcmdline% | find /i "%~f0" >nul && pause
exit /b %RC%
