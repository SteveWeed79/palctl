"""The argv order here is load-bearing (force_install_dir before app_update) and
the ini backup is what saves people's tuning from a validate, so both are
pinned by tests even though the network/subprocess parts aren't."""

import zipfile
from pathlib import Path

from palctl import steamcmd


def test_update_command_order_and_validate():
    cmd = steamcmd.update_command(r"C:\steamcmd\steamcmd.exe", r"C:\PalServer")
    # force_install_dir MUST precede login/app_update or Steam ignores it.
    assert cmd.index("+force_install_dir") < cmd.index("+login") < cmd.index("+app_update")
    assert cmd[cmd.index("+force_install_dir") + 1] == r"C:\PalServer"
    assert cmd[cmd.index("+app_update") + 1] == steamcmd.APP_ID
    assert "validate" in cmd
    assert cmd[-1] == "+quit"


def test_update_command_without_validate():
    cmd = steamcmd.update_command("steamcmd", "dir", app_id="123", validate=False)
    assert "validate" not in cmd
    assert cmd[cmd.index("+app_update") + 1] == "123"
    assert "anonymous" in cmd


def test_extract_steamcmd_top_level(tmp_path: Path):
    zpath = tmp_path / "steamcmd.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("steamcmd.exe", b"MZ")
        z.writestr("readme.txt", "hi")

    exe = steamcmd.extract_steamcmd(zpath, tmp_path / "out")
    assert exe.name == "steamcmd.exe"
    assert exe.exists()


def test_extract_steamcmd_nested(tmp_path: Path):
    zpath = tmp_path / "steamcmd.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("steamcmd/steamcmd.exe", b"MZ")

    exe = steamcmd.extract_steamcmd(zpath, tmp_path / "out")
    assert exe.name == "steamcmd.exe"
    assert exe.exists()


def test_extract_steamcmd_missing_raises(tmp_path: Path):
    zpath = tmp_path / "bad.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("notit.txt", "x")

    try:
        steamcmd.extract_steamcmd(zpath, tmp_path / "out")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError when steamcmd.exe is absent")


def test_backup_file_roundtrip(tmp_path: Path):
    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text("OptionSettings=(ExpRate=1.0)", encoding="utf-8")

    bak = steamcmd.backup_file(ini)
    assert bak is not None and bak.exists()
    assert bak.name.startswith("PalWorldSettings.ini.")
    assert bak.name.endswith(".bak")
    assert bak.read_text(encoding="utf-8") == ini.read_text(encoding="utf-8")


def test_backup_file_missing_is_none(tmp_path: Path):
    assert steamcmd.backup_file(tmp_path / "nope.ini") is None


def test_parse_progress_extracts_percent():
    line = "Update state (0x61) downloading, progress: 42.34 (1234 / 5678)"
    assert steamcmd.parse_progress(line) == 42.34


def test_parse_progress_none_for_ordinary_lines():
    assert steamcmd.parse_progress("Success! App '2394010' fully installed.") is None
    assert steamcmd.parse_progress("") is None
