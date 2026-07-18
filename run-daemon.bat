@echo off
REM Runs the palctl daemon in the foreground (for testing).
REM For production, register the service - see README.
cd /d "%~dp0"
if not exist .venv\ (
  echo Creating venv...
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\python -m palctl.daemon
pause
