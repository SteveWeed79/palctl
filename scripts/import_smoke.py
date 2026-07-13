"""
Import every palctl module, including the GUI/daemon/bot that the unit tests
deliberately skip.

The test suite covers the platform-neutral core under minimal deps; it never
imports the GUI, daemon, or bot, so a broken relative import or a missing symbol
in those files could ship silently. This is the cheap backstop: with the full
dependencies installed and an offscreen Qt platform, importing every module at
least proves the whole package loads.

Run headless with:
    QT_QPA_PLATFORM=offscreen PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
        python scripts/import_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Work whether or not palctl is pip-installed: a bare `python scripts/import_smoke.py`
# puts scripts/ on sys.path, not the repo root, so add the root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODULES = [
    "palctl",
    "palctl.config",
    "palctl.api",
    "palctl.inifile",
    "palctl.backups",
    "palctl.events",
    "palctl.procs",
    "palctl.scheduler",
    "palctl.watchdog",
    "palctl.discovery",
    "palctl.steamcmd",
    "palctl.winservice",
    "palctl.serversetup",
    "palctl.preflight",
    "palctl.netinfo",
    "palctl.localauth",
    "palctl.diagnostics",
    "palctl.selfupdate",
    "palctl.logging_setup",
    "palctl.bot",
    "palctl.daemon",
    "palctl.gui.main",
    "palctl.gui.settings_editor",
    "palctl.gui.wizard",
]


def main() -> int:
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:  # report all failures, don't stop at the first
            failed.append((name, e))
            print(f"FAIL {name}: {e.__class__.__name__}: {e}")
    if failed:
        print(f"\n{len(failed)}/{len(MODULES)} modules failed to import")
        return 1
    print(f"OK: all {len(MODULES)} modules import")
    return 0


if __name__ == "__main__":
    sys.exit(main())
