"""
PyInstaller entry point for the palctl command-line client.

Same reason as daemon_entry.py: the package's relative imports need a proper
package context, so the frozen exe starts here and hands off.
"""

import sys

from palctl.cli import main
from palctl.savescan import frozen_entry

if __name__ == "__main__":
    # A save read runs in its own process so a multi-gigabyte parse can be
    # OOM-killed without taking this one with it. When frozen there is no
    # `-m`, so the child is this same exe started with a marker argument.
    _code = frozen_entry(sys.argv[1:])
    if _code is not None:
        raise SystemExit(_code)
    main()
