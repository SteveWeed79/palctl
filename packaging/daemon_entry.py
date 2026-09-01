"""
PyInstaller entry point for the daemon.

A frozen build can't use ``palctl/daemon.py`` directly as its script: run as
``__main__`` its ``from . import ...`` relative imports have no package to
resolve against. This thin wrapper imports the package properly and hands off.
"""

import sys

from palctl.daemon import main
from palctl.savescan import frozen_entry

if __name__ == "__main__":
    # A save read runs in its own process so a multi-gigabyte parse can be
    # OOM-killed without taking this one with it. When frozen there is no
    # `-m`, so the child is this same exe started with a marker argument.
    _code = frozen_entry(sys.argv[1:])
    if _code is not None:
        raise SystemExit(_code)
    main()
