"""
Check the console scripts pyproject declares — `palctl`, `palctl-daemon`,
`palctl-gui` — against what pip actually installed.

Nothing in CI ran them. Every job reaches the daemon as `python -m palctl.daemon`,
which resolves a module and never touches the entry points, so the three commands
users actually type were verified by nobody. That is not theoretical: the
lifecycle CLI was split out of daemon.py into daemoncli.py, and
`palctl-daemon = "palctl.daemon:main"` now works only because daemon.py
re-exports `main` at the bottom of the file. Delete that re-export and CI stays
green while `palctl-daemon` dies for every installed user.

The list is read from the installed distribution's metadata rather than written
down here, so an entry point added to pyproject is covered by having been added.

Run after `pip install -e .`:
    QT_QPA_PLATFORM=offscreen PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
        python scripts/entrypoint_smoke.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution

# Entry points safe to actually execute: both parse args and exit 0 on
# --version. palctl-gui is resolved but not run — its main() opens a Qt window
# and never returns, so "does it start" is not a question a CI step can ask it.
_RUNNABLE = {"palctl", "palctl-daemon"}


def main() -> int:
    # Read them off the installed distribution, not the whole environment: this
    # asks "what did pip install for palctl", which is the question.
    try:
        dist = distribution("palctl")
    except PackageNotFoundError:
        print("FAIL palctl is not installed — run `pip install -e .` first")
        return 1
    eps = [ep for ep in dist.entry_points if ep.group == "console_scripts"]
    if not eps:
        print("FAIL palctl is installed but declares no console_scripts")
        return 1

    failed = []
    for ep in sorted(eps, key=lambda e: e.name):
        path = shutil.which(ep.name)
        if path is None:
            failed.append(ep.name)
            print(f"FAIL {ep.name}: declared as {ep.value}, but no such command on PATH")
            continue
        try:
            ep.load()  # imports the module and resolves the attribute
        except Exception as e:
            failed.append(ep.name)
            print(f"FAIL {ep.name}: {ep.value} does not resolve: {e.__class__.__name__}: {e}")
            continue
        if ep.name in _RUNNABLE:
            proc = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=60, check=False
            )
            if proc.returncode != 0:
                failed.append(ep.name)
                print(
                    f"FAIL {ep.name} --version exited {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout).strip()[:300]}"
                )
                continue
            print(f"OK   {ep.name} -> {ep.value} -> {proc.stdout.strip() or '(no output)'}")
        else:
            print(f"OK   {ep.name} -> {ep.value} (resolved; not executed)")

    if failed:
        print(f"\n{len(failed)}/{len(eps)} console scripts are broken: {', '.join(failed)}")
        return 1
    print(f"\nOK: all {len(eps)} declared console scripts resolve and run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
