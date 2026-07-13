"""
PyInstaller entry point for the palctl command-line client.

Same reason as daemon_entry.py: the package's relative imports need a proper
package context, so the frozen exe starts here and hands off.
"""

from palctl.cli import main

if __name__ == "__main__":
    main()
