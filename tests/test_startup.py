"""The Run-key COMMAND is the part that has to be exactly right — a wrong command
means the daemon silently never starts at login. The registry writes themselves
are winreg (Windows-only); the command builder is pure and pinned here."""

from palctl import startup


def test_startup_command_frozen_exe():
    # Frozen build: service_target() gives the daemon exe and empty args.
    cmd = startup.startup_command(r"C:\Program Files\palctl\palctl-daemon.exe")
    assert cmd == r'"C:\Program Files\palctl\palctl-daemon.exe" run --headless'


def test_startup_command_dev_checkout_with_args():
    # Dev: python + "-m palctl.daemon". The exe is quoted (may contain spaces).
    cmd = startup.startup_command(r"C:\Python312\python.exe", "-m palctl.daemon")
    assert cmd == r'"C:\Python312\python.exe" -m palctl.daemon run --headless'


def test_startup_command_without_headless():
    assert startup.startup_command("palctl-daemon.exe", headless=False) == (
        '"palctl-daemon.exe" run'
    )


def test_run_value_name_is_stable():
    # The value name is the identity of our autostart entry; changing it would
    # orphan existing installs' entries. Pin it.
    assert startup.RUN_VALUE == "palctl-daemon"
    assert startup.RUN_KEY.endswith(r"CurrentVersion\Run")
