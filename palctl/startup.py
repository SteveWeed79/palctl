"""
Password-free background startup via the current user's Run key.

Running the daemon as a Windows *service under a user account* needs that account
to have a real password and the "log on as a service" right. PIN-only /
Microsoft-account / passwordless home users have neither, so the service manager refuses
with **Error 1069 (logon failure)** — which would break palctl out of the box for
a large share of the target audience.

Registering the daemon in ``HKCU\\...\\Run`` instead sidesteps the service
manager entirely. It starts the daemon in the user's own session at login, with
full access to their ``%APPDATA%`` config and DPAPI-encrypted secrets (the
Discord token), and needs **no password**.

The one tradeoff versus a real service: it only runs while that user is logged
in. A LocalSystem service starts on boot without a login — better for a truly
headless box — which is why palctl keeps both and lets the wizard choose.

The command string is built by a pure function so it's testable off Windows; the
actual registry writes are Windows-only (winreg) and imported lazily.
"""

from __future__ import annotations

# HKEY_CURRENT_USER so no elevation is needed and it's scoped to this user.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "palctl-daemon"


def startup_command(exe: str, args: str = "", *, headless: bool = True) -> str:
    """The command line written to the Run key. Pure — the exe/args come from
    daemon.service_target(), so this works for both the frozen palctl-daemon.exe
    and a `python -m palctl.daemon` dev checkout.

    `run --headless` starts the daemon with its console window hidden, so login
    doesn't flash a black box.
    """
    cmd = f'"{exe}"'
    if args:
        cmd += f" {args}"
    cmd += " run"
    if headless:
        cmd += " --headless"
    return cmd


def install_startup(exe: str, args: str = "") -> str:
    """Write the Run-key value for the current user. Returns the command written.
    Windows-only."""
    import winreg

    cmd = startup_command(exe, args)
    # CreateKeyEx, not OpenKey: the Run key exists on any lived-in profile,
    # but a pristine one (a brand-new Windows account, a CI runner) may not
    # have it yet — OpenKey then raises FileNotFoundError. Open-or-create.
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, cmd)
    return cmd


def uninstall_startup() -> None:
    """Remove the Run-key value. No-op if it isn't there. Windows-only."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def is_startup_installed() -> bool:
    """Whether the Run-key value exists for the current user. Windows-only."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False
