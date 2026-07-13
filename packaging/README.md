# Packaging palctl for Windows

This turns the source tree into a double-click installer (`palctl-setup.exe`) so
end users never touch Python, NSSM, or a terminal. It's a **Windows** build step
and isn't run by CI.

## What's here

| File | Purpose |
|------|---------|
| `daemon_entry.py`, `gui_entry.py`, `cli_entry.py` | PyInstaller entry points. Thin wrappers so the frozen exes import `palctl` as a package (relative imports break if you point PyInstaller straight at `palctl/daemon.py`). |
| `palctl.spec` | PyInstaller spec. Builds `palctl-daemon.exe` (console), `palctl-gui.exe` (windowed), and `palctl.exe` (the CLI) into one `dist\palctl\` folder. |
| `installer.iss` | Inno Setup script. Installs the binaries, adds Start-Menu shortcuts, and optionally registers the palctl background service. |
| `build.ps1` | Does both steps end to end. |

## Build

```powershell
# From the repo root, on Windows:
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Needs [Python 3.11+](https://www.python.org/) and, for the installer,
[Inno Setup 6](https://jrsoftware.org/isdl.php). Without Inno Setup the script
still produces `dist\palctl\` — a self-contained folder you can zip and ship.

The installer lands in `packaging\Output\palctl-setup.exe`.

## What the installer does (and doesn't)

- Installs `palctl-daemon.exe`, `palctl-gui.exe`, and the `palctl.exe` CLI to
  `Program Files\palctl`.
- Adds Start-Menu (and optional desktop) shortcuts, and can optionally add the
  install dir to the PATH so `palctl` works in any terminal.
- Optionally runs `palctl-daemon.exe install-service`, which downloads NSSM and
  registers **palctl-daemon** as an always-on Windows service.
- Launches the GUI, whose **first-run wizard** finishes the job: auto-detect the
  server, turn on the REST API, optionally install the server with SteamCMD, and
  register the **PalServer** service.

The installer deliberately does *not* register the Palworld server service
itself — at install time it doesn't know where (or whether) the server is
installed. That's the wizard's job, once paths are known.

## Headless / scripted service registration

No GUI needed:

```powershell
palctl-daemon.exe install-service      # register + start the daemon service
palctl-daemon.exe uninstall-service    # remove it
```

## If the frozen build hits a `ModuleNotFoundError`

PyInstaller occasionally misses a dynamically-imported backend. Add the module
to `hiddenimports` in `palctl.spec` and rebuild — `keyring` and `PySide6`
backends are the usual culprits and are already listed there.
