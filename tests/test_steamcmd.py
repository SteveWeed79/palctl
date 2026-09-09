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


@pytest.fixture(autouse=True)
def _cache_confined_to_tmp(monkeypatch):
    """latest_buildid deletes SteamCMD's app-info cache before asking Steam.
    Confine it to the fake SteamCMD's own directory here, so no test can ever
    reach into the developer's real ~/Steam."""
    monkeypatch.setattr(
        steamcmd, "appinfo_cache_paths",
        lambda exe: [Path(exe).parent / steamcmd.APPINFO_CACHE],
    )


def test_default_steamcmd_url_by_platform():
    url = steamcmd.default_steamcmd_url()
    assert url.endswith(".zip") or url.endswith(".tar.gz")


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


# What `steamcmd +app_info_print 2394010` really prints, cut down. Two things
# about its shape matter: every depot lists its manifests *per branch* before
# the `branches` block ever appears, and inside `branches` the order is Steam's
# — here the beta comes first, as it does whenever a branch was created before
# the public one was last touched.
_APP_INFO_DUMP = """\
"2394010"
{
\t"common"
\t{
\t\t"name"\t\t"Palworld Dedicated Server"
\t\t"type"\t\t"Tool"
\t}
\t"depots"
\t{
\t\t"2394011"
\t\t{
\t\t\t"config"
\t\t\t{
\t\t\t\t"oslist"\t\t"windows"
\t\t\t}
\t\t\t"manifests"
\t\t\t{
\t\t\t\t"public"
\t\t\t\t{
\t\t\t\t\t"gid"\t\t"1234567890123456789"
\t\t\t\t\t"size"\t\t"9876543210"
\t\t\t\t}
\t\t\t\t"beta-old"
\t\t\t\t{
\t\t\t\t\t"gid"\t\t"1111111111111111111"
\t\t\t\t}
\t\t\t}
\t\t}
\t\t"branches"
\t\t{
\t\t\t"beta-old"
\t\t\t{
\t\t\t\t"buildid"\t\t"18000000"
\t\t\t\t"description"\t\t"Previous build"
\t\t\t\t"pwdrequired"\t\t"1"
\t\t\t\t"timeupdated"\t\t"1700000000"
\t\t\t}
\t\t\t"public"
\t\t\t{
\t\t\t\t"buildid"\t\t"20087975"
\t\t\t\t"timeupdated"\t\t"1749525254"
\t\t\t}
\t\t}
\t}
}
"""


def test_parse_branch_buildids_reads_the_branches_block():
    assert steamcmd.parse_branch_buildids(_APP_INFO_DUMP) == {
        "beta-old": "18000000",
        "public": "20087975",
    }


def test_parse_latest_buildid_is_not_fooled_by_depot_manifests_or_branch_order():
    """The bug: the first `"public"` in the dump is a depot's manifest entry,
    and the first `"buildid"` after it belongs to whichever branch Steam lists
    first — the beta, here. That read the beta's build as the public one, so
    the check reported an update that didn't exist (or hid one that did)."""
    assert steamcmd.parse_latest_buildid(_APP_INFO_DUMP) == "20087975"


def test_parse_latest_buildid_can_follow_a_held_branch():
    """A server held on `steam_branch` has to be compared against THAT branch's
    build, or it reads as permanently behind the public one."""
    assert steamcmd.parse_latest_buildid(_APP_INFO_DUMP, branch="beta-old") == "18000000"
    assert steamcmd.parse_latest_buildid(_APP_INFO_DUMP, branch="no-such-branch") is None


def test_parse_latest_buildid_is_none_without_a_branches_block():
    """A dump with no `branches` block is a cut-off or failed one. "Don't know"
    is the answer that keeps the auto-update loop failing closed."""
    cut_off = _APP_INFO_DUMP[: _APP_INFO_DUMP.find('"branches"')]
    assert steamcmd.parse_latest_buildid(cut_off) is None
    assert steamcmd.parse_branch_buildids("") == {}


def test_app_info_command_logs_in_anonymously():
    cmd = steamcmd.app_info_command(r"C:\steamcmd\steamcmd.exe", "2394010")
    assert cmd[cmd.index("+login") + 1] == "anonymous"
    assert "+app_info_update" in cmd and cmd[cmd.index("+app_info_update") + 1] == "1"
    assert cmd[cmd.index("+app_info_print") + 1] == "2394010"
    assert cmd[-1] == "+quit"
    # And the update itself never asks for an account either.
    update = steamcmd.update_command("steamcmd", "dir")
    assert update[update.index("+login") + 1] == "anonymous"


# ---------------- the stale app-info cache ----------------
#
# SteamCMD answers `app_info_print` from `appcache/appinfo.vdf`, and
# `+app_info_update 1` does not reliably refresh it. For a SteamCMD that last
# ran to install the current build, the cached "latest" IS the current build —
# so installed == latest, forever, and no update is ever detected.


def test_appinfo_cache_paths_lead_with_steamcmds_own_directory(monkeypatch, tmp_path):
    monkeypatch.undo()  # the autouse fixture replaces this very function
    paths = steamcmd.appinfo_cache_paths(tmp_path / "steamcmd" / "steamcmd.exe")
    assert paths[0] == tmp_path / "steamcmd" / "appcache" / "appinfo.vdf"
    assert all(p.name == "appinfo.vdf" for p in paths)
    assert len(paths) == len({str(p).lower() for p in paths})  # no duplicates


def test_clear_appinfo_cache_removes_the_stale_cache_and_reports_it(tmp_path):
    exe = tmp_path / "steamcmd.sh"
    exe.write_text("#!/bin/sh\n")
    cache = tmp_path / "appcache" / "appinfo.vdf"
    cache.parent.mkdir()
    cache.write_bytes(b"stale")

    assert steamcmd.clear_appinfo_cache(exe) == [cache]
    assert not cache.exists()
    # Nothing to clear is not an error.
    assert steamcmd.clear_appinfo_cache(exe) == []


def _executable(tmp_path: Path, inner: str, name: str = "fake") -> Path:
    """An executable the OS can exec directly (latest_buildid invokes the
    steamcmd path itself, not the interpreter) that runs `inner` in Python and
    ignores SteamCMD's arguments."""
    script = tmp_path / f"{name}.py"
    script.write_text(inner)
    if sys.platform.startswith("win"):
        launcher = tmp_path / f"{name}.bat"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\n')
    else:
        launcher = tmp_path / f"{name}.sh"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
        launcher.chmod(0o755)
    return launcher


def _dump_printer(tmp_path: Path, *, prelude: str = "") -> Path:
    """A fake steamcmd that prints the app-info dump (after `prelude`)."""
    dump = tmp_path / "dump.txt"
    dump.write_text(_APP_INFO_DUMP)
    return _executable(
        tmp_path,
        prelude + f"import sys\nsys.stdout.write(open({str(dump)!r}).read())\n",
    )


def test_latest_buildid_clears_the_cache_and_reads_the_answer(tmp_path):
    exe = _dump_printer(tmp_path)
    cache = tmp_path / "appcache" / "appinfo.vdf"
    cache.parent.mkdir()
    cache.write_bytes(b"the build palctl installed last time")

    assert asyncio.run(steamcmd.latest_buildid(exe, timeout=10.0)) == "20087975"
    assert not cache.exists(), "the stale cache must be gone before SteamCMD runs"
    # ...and a held branch is answered from the same dump.
    assert asyncio.run(
        steamcmd.latest_buildid(exe, branch="beta-old", timeout=10.0)
    ) == "18000000"


def test_latest_buildid_waits_out_a_slow_but_chatty_bootstrap(tmp_path):
    """SteamCMD's first run downloads a new copy of itself before answering —
    slow, but never silent. A plain overall cap killed that mid-bootstrap;
    only *silence* is a hang."""
    chatty = (
        "import sys, time\n"
        "for i in range(6):\n"
        "    print('[%3d%%] Downloading update...' % (i * 16), flush=True)\n"
        "    time.sleep(0.5)\n"
    )
    exe = _dump_printer(tmp_path, prelude=chatty)
    # Three seconds of progress lines against a 1.5 s stall timeout: it must
    # succeed, because it never went quiet for that long.
    assert asyncio.run(steamcmd.latest_buildid(exe, timeout=1.5)) == "20087975"


def test_latest_buildid_gives_up_when_the_run_exceeds_the_ceiling(tmp_path):
    forever = (
        "import time\n"
        "while True:\n"
        "    print('Update state (0x61) downloading, progress: 1.00', flush=True)\n"
        "    time.sleep(0.2)\n"
    )
    exe = _executable(tmp_path, forever)
    t0 = time.monotonic()
    assert asyncio.run(
        steamcmd.latest_buildid(exe, timeout=5.0, total_timeout=2.0)
    ) is None
    assert time.monotonic() - t0 < 30


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


def test_latest_buildid_gives_up_on_a_hung_query(tmp_path):
    """The periodic update check must not park a child process forever: an
    unbounded metadata query leaks a process and stops that loop ticking again.
    A timeout reads as "don't know", the same as every other failure here."""
    hung = _executable(tmp_path, "import time; time.sleep(600)\n", name="hang")
    t0 = time.monotonic()
    assert asyncio.run(steamcmd.latest_buildid(hung, timeout=2.0)) is None
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
