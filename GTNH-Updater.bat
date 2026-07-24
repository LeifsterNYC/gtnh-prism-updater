@echo off
title GTNH Updater
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  where python3 >nul 2>&1 && set "PY=python3"
)

if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 goto nopython

%PY% "%~dp0gtnh-prism-update.py" --setup %*
echo.
pause
exit /b 0

:nopython
echo.
echo   Python 3 is needed and was not found.
echo.
echo   1. Get it from https://www.python.org/downloads/
echo   2. During install, TICK the box "Add python.exe to PATH"
echo   3. Run this file again.
echo.
pause
exit /b 1
