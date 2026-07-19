@echo off
REM ---------------------------------------------------------------------------
REM Runs the palctl daemon in the FOREGROUND from a source checkout — handy for
REM testing or watching the logs live. This is not how you run palctl for real:
REM for an always-on background daemon, install the packaged app or register the
REM service (palctl-daemon install-service). See the README.
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

".venv\Scripts\python" -m palctl.daemon
pause
