@echo off
rem TimeCheat launcher for Windows: double-click it, or run start.bat [options].
rem The first start creates a Python environment in .venv; packages are reinstalled when requirements.txt changes.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto deps
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo TimeCheat needs Python 3.10 or newer: https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" in the installer.
  goto fail
)
echo Creating the Python environment, first start only...
%PY% -m venv .venv || goto fail

:deps
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
  echo Installing the required packages...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto pipfail
  copy /y requirements.txt .venv\requirements.installed >nul
)

".venv\Scripts\python.exe" run.py %*
if errorlevel 1 goto fail
exit /b 0

:pipfail
echo Installing the packages failed. Is https://pypi.org reachable?
:fail
echo.
pause
exit /b 1
