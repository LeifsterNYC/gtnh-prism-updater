@echo off
title GTNH Updater
cd /d "%~dp0"

set "PYVER=3.13.14"

call :findpython
if defined PY goto run

echo.
echo   GTNH Updater needs Python 3, and it isn't installed on this PC.
echo.
choice /C YN /N /M "  Install it now automatically? [Y/N] "
if errorlevel 2 goto manual
echo.

where winget >nul 2>&1
if errorlevel 1 goto download
echo   Installing Python with winget...
winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements
call :findpython
if defined PY goto run

:download
echo   Downloading the official Python installer...
set "PYEXE=%TEMP%\python-%PYVER%-amd64.exe"
curl -L --fail -o "%PYEXE%" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
if errorlevel 1 goto manual
echo   Installing Python — this takes a minute, no clicking needed...
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_test=0
del "%PYEXE%" >nul 2>&1
call :findpython
if defined PY goto run
goto manual

:run
echo.
%PY% "%~dp0gtnh-prism-update.py" --setup %*
echo.
pause
exit /b 0

:manual
echo.
echo   Couldn't install Python automatically.
echo.
echo   1. Get it from https://www.python.org/downloads/
echo   2. During install, TICK the box "Add python.exe to PATH"
echo   3. Run this file again.
echo.
pause
exit /b 1

:findpython
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY exit /b 0
rem Ignore the Microsoft Store stub, which opens the Store instead of running.
set "PYPATH="
for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do set "PYPATH=%%P"
if defined PYPATH (
  "%PYPATH%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
  if not errorlevel 1 set PY="%PYPATH%"
)
if defined PY exit /b 0
rem A fresh install isn't on PATH in this window yet — look where it lands.
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" set PY="%%D\python.exe"
)
exit /b 0
