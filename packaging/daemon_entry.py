"""
PyInstaller entry point for the daemon.

A frozen build can't use ``palctl/daemon.py`` directly as its script: run as
``__main__`` its ``from . import ...`` relative imports have no package to
resolve against. This thin wrapper imports the package properly and hands off.
"""

from palctl.daemon import main

if __name__ == "__main__":
    main()
