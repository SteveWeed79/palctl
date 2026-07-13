# PyInstaller spec: builds palctl-daemon.exe (console) and palctl-gui.exe
# (windowed) into a single dist\palctl\ folder, so end users never install
# Python. Build with:  pyinstaller --noconfirm --clean packaging\palctl.spec
#
# Design notes for reliability (this can't be test-run off Windows, so it's
# built to fail as rarely as possible):
#   * No MERGE(). MERGE de-duplicates shared libraries between the two exes but
#     is finicky about ordering and paths; here we run two independent Analyses
#     and let COLLECT de-duplicate by destination instead. Slightly larger build
#     graph, materially fewer surprises.
#   * keyring and discord both choose/what-to-load at runtime, which PyInstaller's
#     static import scan can miss — so we pull in ALL their submodules explicitly.
#
# If a frozen build still hits a runtime "ModuleNotFoundError", the message names
# the module exactly: add it to `hiddenimports` and rebuild.

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    # keyring selects its backend dynamically (Windows Credential Locker via
    # win32ctypes at runtime); collect every backend so the right one is present.
    *collect_submodules("keyring"),
    # discord.py loads subpackages lazily.
    *collect_submodules("discord"),
    # pywin32 timezone helper the keyring Windows backend can reach for.
    "win32timezone",
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

# One folder with both exes; COLLECT de-duplicates the shared Qt / Python runtime.
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
