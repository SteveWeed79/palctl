# PyInstaller spec: builds palctl-daemon.exe (console) and palctl-gui.exe
# (windowed) into a single dist\palctl\ folder, so end users never install
# Python. Build with:  pyinstaller --noconfirm --clean packaging\palctl.spec
#
# This is a Windows build step and is not exercised by CI — if a runtime
# "ModuleNotFoundError" shows up in the frozen build, add the offending module
# to `hiddenimports` below (keyring's and PySide6's backends are the usual
# suspects, which is why they're already listed).

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    "keyring.backends.Windows",       # DPAPI-backed secret storage
    "keyring.backends.SecretService",
    "win32timezone",                  # pulled in by pywin32 via keyring
    *collect_submodules("discord"),
]

daemon_a = Analysis(
    ["daemon_entry.py"],
    pathex=[".."],
    hiddenimports=hiddenimports,
    noarchive=False,
)
gui_a = Analysis(
    ["gui_entry.py"],
    pathex=[".."],
    hiddenimports=hiddenimports,
    noarchive=False,
)

MERGE(
    (daemon_a, "daemon_entry", "palctl-daemon"),
    (gui_a, "gui_entry", "palctl-gui"),
)

daemon_pyz = PYZ(daemon_a.pure)
daemon_exe = EXE(
    daemon_pyz,
    daemon_a.scripts,
    [],
    exclude_binaries=True,
    name="palctl-daemon",
    console=True,   # headless service; keep a console for logs
)

gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="palctl-gui",
    console=False,  # windowed
)

COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.zipfiles,
    daemon_a.datas,
    gui_exe,
    gui_a.binaries,
    gui_a.zipfiles,
    gui_a.datas,
    name="palctl",
)
