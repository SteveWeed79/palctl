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

# The Windows app icon (green brand tile), committed by packaging/make_icon.py
# and embedded in every exe so the taskbar/Explorer show it.
APP_ICON = "app-icon.ico"

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
    # The web dashboard the daemon serves at /; daemon.py resolves it next to
    # its own module, so it must land inside the frozen palctl/ package dir.
    datas=[("../palctl/dashboard.html", "palctl")],
    noarchive=False,
)
gui_a = Analysis(
    ["gui_entry.py"],
    pathex=[".."],
    hiddenimports=hiddenimports,
    # The icon set; palctl/gui/icons.py resolves it next to its own module
    # (Path(__file__).with_name("icons")), so it must land inside the frozen
    # palctl/gui/ package dir.
    datas=[("../palctl/gui/icons/*.svg", "palctl/gui/icons")],
    noarchive=False,
)
# The command-line client. It only needs httpx + keyring, but it shares the
# full hiddenimports list — COLLECT de-duplicates against the daemon anyway,
# and one list means one place to fix a missing module.
cli_a = Analysis(
    ["cli_entry.py"],
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
    icon=APP_ICON,
)

gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="palctl-gui",
    console=False,  # windowed
    icon=APP_ICON,
)

cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name="palctl",
    console=True,   # it IS a console program
    icon=APP_ICON,
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
    cli_exe,
    cli_a.binaries,
    cli_a.zipfiles,
    cli_a.datas,
    name="palctl",
)
