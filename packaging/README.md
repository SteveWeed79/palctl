# Packaging palctl for Windows

This turns the source tree into a double-click installer (`palctl-setup.exe`) so
end users never touch Python, NSSM, or a terminal. It's a **Windows** build step
and isn't run by CI.

## What's here

| File | Purpose |
|------|---------|
| `daemon_entry.py`, `gui_entry.py`, `cli_entry.py` | PyInstaller entry points. Thin wrappers so the frozen exes import `palctl` as a package (relative imports break if you point PyInstaller straight at `palctl/daemon.py`). |
| `palctl.spec` | PyInstaller spec. Builds `palctl-daemon.exe` (console), `palctl-gui.exe` (windowed), and `palctl.exe` (the CLI) into one `dist\palctl\` folder. |
| `installer.iss` | Inno Setup script. Installs the binaries, adds Start-Menu shortcuts, and carries the hash-verified WinSW wrapper + VC++ runtime so setup never downloads. Registers no service itself — that's the wizard's job (see below). |
| `build.ps1` | Does both steps end to end. |
| `make_icon.py` | Regenerates the committed app icon (`app-icon.ico` here + `app-icon-tile.svg` in the package) from `palctl/gui/icons/app-icon.svg`. Run only when the app glyph changes; needs `cairosvg`. |
| `app-icon.ico` | The Windows app icon embedded in the exes (`palctl.spec`) and used by the installer (`installer.iss`). Committed so the build needs no rasteriser. |

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
- Installs the Visual C++ x64 runtime if it's missing (it ships *inside* the
  installer, Authenticode-verified at build time — nothing is downloaded at
  install time). The Palworld server won't launch without it.
- Launches the GUI, whose **first-run wizard** finishes the job: auto-detect the
  server, turn on the REST API, optionally install the server with SteamCMD, and
  register **both** the **PalServer** and **palctl-daemon** Windows services
  under your account (see "Path A" in [../docs/install-design.md](../docs/install-design.md)).

The installer deliberately registers **no service itself** — not the daemon and
not the server. Registering the daemon here could only produce a LocalSystem
daemon with no config (a half-setup that fights the wizard), and at install time
it doesn't yet know where the server lives. Both services are the wizard's job,
once paths and the account password are known; unattended deployments script
`palctl-daemon install-service` instead. The service wrapper (WinSW) also ships
inside the build, SHA-256-verified — so, like the VC++ runtime, it is never
downloaded during setup.

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
