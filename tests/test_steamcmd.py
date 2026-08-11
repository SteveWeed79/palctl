"""The argv order here is load-bearing (force_install_dir before app_update) and
the ini backup is what saves people's tuning from a validate, so both are
pinned by tests. The download isn't exercised, but the process runners are: a
SteamCMD that stops responding used to wedge the daemon permanently, so the
stall guard at the bottom of this file drives real child processes."""

import asyncio
import io
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import psutil
import pytest

from palctl import steamcmd


def test_default_steamcmd_url_by_platform():
    url = steamcmd.default_steamcmd_url()
    assert url.endswith((".zip", ".tar.gz"))


def test_extract_steamcmd_from_targz(tmp_path: Path):
    tgz = tmp_path / "steamcmd_linux.tar.gz"
    payload = b"#!/bin/sh\n"
    with tarfile.open(tgz, "w:gz") as t:
        info = tarfile.TarInfo("steamcmd.sh")
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))

    out = steamcmd.extract_steamcmd(tgz, tmp_path / "out")
    assert out.name == "steamcmd.sh" and out.exists()


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


def test_parse_installed_buildid():
    acf = '"AppState"\n{\n\t"appid"\t"2394010"\n\t"buildid"\t"12345678"\n}'
    assert steamcmd.parse_installed_buildid(acf) == "12345678"
    assert steamcmd.parse_installed_buildid("nothing here") is None


def test_parse_latest_buildid_reads_public_branch():
    txt = (
        '"branches"\n{\n'
        '  "public" { "buildid" "999" "timeupdated" "170000" }\n'
        '  "beta"   { "buildid" "111" }\n'
        "}"
    )
    assert steamcmd.parse_latest_buildid(txt) == "999"
    # No public branch -> no answer, rather than guessing the wrong branch.
    assert steamcmd.parse_latest_buildid('"branches" { "beta" { "buildid" "1" } }') is None


# ---------------- stall guard ----------------
#
# A SteamCMD that never exits used to hang the daemon permanently: the update
# holds the one server-operation lock and stops the game server *before*
# running SteamCMD, so a wedged run leaves the server down and every
# start/stop/restart/backup/restore answering "busy: update is in progress"
# until someone restarts the daemon by hand.
#
# These drive real child processes rather than fakes, because the two bugs
# worth pinning are both about real process/pipe behaviour: killing only our
# direct child leaves a grandchild holding the stdout pipe, and asyncio's
# Process.wait() then never returns.

# A fake steamcmd: print a line, then sit forever. Written as a Python script so
# it behaves the same on Windows and Linux.
_STALL_SCRIPT = (
    "import sys, time\n"
    "print(' Update state (0x61) downloading, progress: 12.34', flush=True)\n"
    "time.sleep(600)\n"
)

# The same, but it first spawns a child that inherits stdout and outlives it,
# and reports that child's pid so the test can check on it. This is the shape of
# the real thing (steamcmd.sh re-execs linux32/steamcmd; the Windows build
# spawns helpers) and the case that makes a naive kill useless.
_STALL_WITH_CHILD_SCRIPT = (
    "import subprocess, sys, time\n"
    "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "print('CHILDPID %d' % kid.pid, flush=True)\n"
    "print(' Update state (0x61) downloading, progress: 12.34', flush=True)\n"
    "time.sleep(600)\n"
)

_OK_SCRIPT = (
    "import sys\n"
    "print('Update state (0x61) downloading, progress: 50.00', flush=True)\n"
    "print(\"Success! App '2394010' fully installed.\", flush=True)\n"
    "sys.exit(0)\n"
)


def _fake_steamcmd(tmp_path: Path, script: str) -> Path:
    """A stand-in steamcmd. update_command() puts its own args after the binary;
    the script ignores them, which is what we want."""
    p = tmp_path / "fake_steamcmd.py"
    p.write_text(script)
    return p


def _patch_argv(monkeypatch, script_path: Path):
    """Run the fake through the current interpreter, ignoring SteamCMD's argv."""
    monkeypatch.setattr(
        steamcmd, "update_command",
        lambda *a, **k: [sys.executable, str(script_path)],
    )


def test_run_update_async_raises_when_steamcmd_goes_silent(tmp_path, monkeypatch):
    _patch_argv(monkeypatch, _fake_steamcmd(tmp_path, _STALL_SCRIPT))
    t0 = time.monotonic()
    with pytest.raises(steamcmd.SteamCmdStalled):
        asyncio.run(
            steamcmd.run_update_async(
                "steamcmd", tmp_path / "srv", stall_timeout=2.0
            )
        )
    # It must actually give up on the stall timer, not wait out the process.
    assert time.monotonic() - t0 < 30


def _child_pid_from(lines: list[str]) -> int:
    for line in lines:
        if line.startswith("CHILDPID "):
            return int(line.split()[1])
    raise AssertionError(f"fake steamcmd never reported its child pid: {lines}")


def test_run_update_async_stall_kill_reaches_the_whole_process_tree(
    tmp_path, monkeypatch
):
    """The regression that matters: kill the descendants, not just our child.

    A grandchild that inherited the stdout pipe holds it open after its parent
    dies, so the reader never sees EOF and asyncio's `proc.wait()` blocks for as
    long as that orphan lives — re-creating the permanent hang the stall timeout
    exists to prevent. Leaving it running also means SteamCMD is still writing
    into the install directory while the daemon restarts the server on top of it.
    """
    _patch_argv(monkeypatch, _fake_steamcmd(tmp_path, _STALL_WITH_CHILD_SCRIPT))
    lines: list[str] = []
    t0 = time.monotonic()
    with pytest.raises(steamcmd.SteamCmdStalled):
        asyncio.run(
            steamcmd.run_update_async(
                "steamcmd", tmp_path / "srv", stall_timeout=2.0, on_line=lines.append
            )
        )
    assert time.monotonic() - t0 < 30

    kid = _child_pid_from(lines)
    # The orphan must be gone (a zombie counts: its file descriptors, and so the
    # inherited pipe, are already released).
    assert not psutil.pid_exists(kid) or psutil.Process(kid).status() == psutil.STATUS_ZOMBIE


def test_run_update_async_still_returns_the_exit_code_on_a_normal_run(
    tmp_path, monkeypatch
):
    """The stall guard must not disturb the happy path — every line still
    reaches on_line, and the real exit code comes back."""
    _patch_argv(monkeypatch, _fake_steamcmd(tmp_path, _OK_SCRIPT))
    lines: list[str] = []
    code = asyncio.run(
        steamcmd.run_update_async(
            "steamcmd", tmp_path / "srv", stall_timeout=30.0, on_line=lines.append
        )
    )
    assert code == 0
    assert any("fully installed" in line for line in lines)


def test_run_update_sync_raises_when_steamcmd_goes_silent(tmp_path, monkeypatch):
    """Same guard on the blocking runner the setup wizard uses — without it the
    wizard sits on 'Installing the server…' with no way out but killing the app."""
    _patch_argv(monkeypatch, _fake_steamcmd(tmp_path, _STALL_WITH_CHILD_SCRIPT))
    t0 = time.monotonic()
    with pytest.raises(steamcmd.SteamCmdStalled):
        steamcmd.run_update(
            "steamcmd", tmp_path / "srv", stall_timeout=2.0
        )
    assert time.monotonic() - t0 < 30


def test_run_update_sync_still_returns_the_exit_code_on_a_normal_run(
    tmp_path, monkeypatch
):
    _patch_argv(monkeypatch, _fake_steamcmd(tmp_path, _OK_SCRIPT))
    lines: list[str] = []
    assert steamcmd.run_update(
        "steamcmd", tmp_path / "srv", stall_timeout=30.0, on_line=lines.append
    ) == 0
    assert any("fully installed" in line for line in lines)


def _hanging_executable(tmp_path: Path) -> Path:
    """An executable that ignores its arguments and hangs. latest_buildid()
    invokes the steamcmd path directly (not through the interpreter), so this
    has to be something the OS can exec on its own."""
    inner = tmp_path / "hang.py"
    inner.write_text("import time; time.sleep(600)\n")
    if sys.platform.startswith("win"):
        launcher = tmp_path / "hang.bat"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{inner}"\r\n')
    else:
        launcher = tmp_path / "hang.sh"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{inner}"\n')
        launcher.chmod(0o755)
    return launcher


def test_latest_buildid_gives_up_on_a_hung_query(tmp_path):
    """The six-hourly update check must not park a child process forever: an
    unbounded metadata query leaks a process and stops that loop ticking again.
    A timeout reads as "don't know", the same as every other failure here."""
    t0 = time.monotonic()
    assert asyncio.run(
        steamcmd.latest_buildid(_hanging_executable(tmp_path), timeout=2.0)
    ) is None
    assert time.monotonic() - t0 < 30


def test_stall_duration_reads_naturally():
    assert steamcmd._stall_duration(600) == "10 minutes"
    assert steamcmd._stall_duration(45) == "45 seconds"
ACF = '"AppState"\n{\n\t"appid"\t"2394010"\n\t"buildid"\t"777"\n}'


def test_manifest_found_in_force_install_dir_layout(tmp_path: Path):
    # SteamCMD with +force_install_dir keeps the manifest inside the server root.
    root = tmp_path / "PalServer"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "appmanifest_2394010.acf").write_text(ACF)

    assert steamcmd.manifest_path(root) == root / "steamapps" / "appmanifest_2394010.acf"
    assert steamcmd.installed_buildid(root) == "777"


def test_manifest_found_in_steam_library_layout(tmp_path: Path):
    # A Steam-client install: the game is in <lib>/steamapps/common/PalServer and
    # the manifest sits two levels up. Reading only the first layout made the
    # build id 'unknown' for these installs, so the update check never fired.
    lib = tmp_path / "Steam"
    root = lib / "steamapps" / "common" / "PalServer"
    root.mkdir(parents=True)
    (lib / "steamapps" / "appmanifest_2394010.acf").write_text(ACF)

    assert steamcmd.manifest_path(root) == lib / "steamapps" / "appmanifest_2394010.acf"
    assert steamcmd.installed_buildid(root) == "777"


def test_manifest_absent_reads_as_unknown(tmp_path: Path):
    root = tmp_path / "PalServer"
    root.mkdir()
    assert steamcmd.manifest_path(root) is None
    assert steamcmd.installed_buildid(root) is None


def test_manifest_walk_up_stops_before_unrelated_installs(tmp_path: Path):
    # A manifest further up than steamapps/common/<game> belongs to a different
    # install; picking it up would report someone else's build id as ours.
    root = tmp_path / "a" / "b" / "c" / "d" / "PalServer"
    root.mkdir(parents=True)
    (tmp_path / "steamapps").mkdir()
    (tmp_path / "steamapps" / "appmanifest_2394010.acf").write_text(ACF)

    assert steamcmd.manifest_path(root) is None


# ---------------- archive extraction stays inside the destination ----------------


def _tar_with(path: Path, names, *, symlink: str | None = None) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for name in names:
            data = b"x"
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        if symlink:
            link = tarfile.TarInfo(symlink)
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            t.addfile(link)
    return path


def test_tar_members_outside_the_destination_are_dropped(tmp_path: Path):
    """`requires-python` is >=3.11 but tarfile's `data` filter only landed in
    3.11.4, and the fallback for the versions below it was a plain extractall —
    which honours `../..` and absolute member names. _members_within is what
    keeps that fallback from writing outside the install directory."""
    archive = _tar_with(
        tmp_path / "s.tar.gz",
        ["../../escaped.txt", "/abs.txt", "steamcmd.sh", "sub/ok.txt"],
        symlink="link",
    )
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(archive) as t:
        kept = [m.name for m in steamcmd._members_within(t, dest)]

    assert sorted(kept) == ["steamcmd.sh", "sub/ok.txt"]
    assert "../../escaped.txt" not in kept
    assert "/abs.txt" not in kept
    assert "link" not in kept  # a link is a second chance to escape, once followed


def test_tar_extraction_writes_nothing_outside_the_destination(tmp_path: Path):
    archive = _tar_with(tmp_path / "s.tar.gz", ["../../escaped.txt", "steamcmd.sh"])
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(archive) as t:
        t.extractall(dest, members=steamcmd._members_within(t, dest))

    assert (dest / "steamcmd.sh").is_file()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_zip_extraction_cannot_escape_the_destination(tmp_path: Path):
    """Python's ZipFile strips the drive, leading separators and every '..'
    component before joining, so the zip path needs no filter of its own — this
    pins the behaviour the code relies on."""
    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../../escaped.txt", "x")
        z.writestr("/abs.txt", "x")
        z.writestr("steamcmd.exe", "x")
    dest = tmp_path / "dest"

    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)

    assert not (tmp_path.parent / "escaped.txt").exists()
    assert {p.name for p in dest.rglob("*") if p.is_file()} == {
        "escaped.txt", "abs.txt", "steamcmd.exe",
    }
