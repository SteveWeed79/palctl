@echo off
REM Opens the palctl GUI. Safe to close - the daemon keeps running.
cd /d "%~dp0"
if not exist .venv\ (
  echo Creating venv...
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw -m palctl.gui.main
