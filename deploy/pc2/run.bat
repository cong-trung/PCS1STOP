@echo off
rem Runs the PCS 1-Stop worker in this window (Ctrl+C stops it).
rem   run.bat            run the worker
rem   run.bat --check    only test the settings and the connection to PC1
cd /d "%~dp0"
set "PYCMD="
call :probe "py -3"
if not defined PYCMD call :probe python
if not defined PYCMD call :probe python3
if not defined PYCMD (
    echo Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    set RC=1
    goto :done
)
call %PYCMD% worker_agent.py %*
set RC=%errorlevel%
:done
echo.
rem started by a double click: keep the window open so the messages can be read (set PCS_NOPAUSE=1 to never wait)
if not defined PCS_NOPAUSE echo %cmdcmdline% | find /i "%~f0" >nul && pause
exit /b %RC%

:probe
rem sets PYCMD to the given command when it starts Python 3.10 or newer (a Microsoft Store stub or an old Python does not);
rem "call" so that a python.bat shim (pyenv-win ...) hands control back
call %~1 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=%~1"
goto :eof
