@echo off
REM ---------------------------------------------------------------------------
REM Opens the palctl GUI from a source checkout. Safe to close — the daemon keeps
REM running in the background. (The packaged installer gives you a Start-Menu
REM shortcut for this; the script is only for running from source.)
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on your PATH. Install Python 3.11+ from
  echo https://www.python.org/ ^(tick "Add python.exe to PATH"^), then re-run this.
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo Creating a virtual environment in .venv ...
  python -m venv .venv || (echo Could not create the venv. & pause & exit /b 1)
  ".venv\Scripts\python" -m pip install --upgrade pip
  ".venv\Scripts\pip" install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)
)

start "" ".venv\Scripts\pythonw" -m palctl.gui.main
