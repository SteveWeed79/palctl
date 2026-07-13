"""NSSM registration is Windows-only, but the command sequence and the archive
layout logic are the parts that go wrong silently, so those are pinned here."""

from pathlib import Path

from palctl import winservice


def test_install_commands_full():
    cmds = winservice.install_commands(
        "nssm.exe", "palctl-daemon", r"C:\app\palctl-daemon.exe",
        args="-m palctl.daemon", app_dir=r"C:\app",
    )
    assert cmds[0] == ["nssm.exe", "install", "palctl-daemon", r"C:\app\palctl-daemon.exe"]
    joined = [" ".join(c) for c in cmds]
    assert any("AppParameters" in j and "palctl.daemon" in j for j in joined)
    assert any("AppDirectory" in j for j in joined)
    # Auto-start is always configured, and last.
    assert cmds[-1] == ["nssm.exe", "set", "palctl-daemon", "Start", "SERVICE_AUTO_START"]


def test_install_commands_minimal_still_sets_autostart():
    cmds = winservice.install_commands("nssm.exe", "svc", "svc.exe")
    assert cmds[0] == ["nssm.exe", "install", "svc", "svc.exe"]
    assert ["nssm.exe", "set", "svc", "Start", "SERVICE_AUTO_START"] in cmds
    # No args and no app_dir -> only install + the autostart set.
    assert len(cmds) == 2


def test_nssm_exe_in_prefers_arch(tmp_path: Path):
    for arch in ("win32", "win64"):
        d = tmp_path / "nssm-2.24" / arch
        d.mkdir(parents=True)
        (d / "nssm.exe").write_bytes(b"MZ")

    got64 = winservice.nssm_exe_in(tmp_path, win64=True)
    assert got64 is not None and got64.parent.name == "win64"
    got32 = winservice.nssm_exe_in(tmp_path, win64=False)
    assert got32 is not None and got32.parent.name == "win32"


def test_nssm_exe_in_falls_back_to_any(tmp_path: Path):
    d = tmp_path / "weird"
    d.mkdir()
    (d / "nssm.exe").write_bytes(b"MZ")
    assert winservice.nssm_exe_in(tmp_path, win64=True) is not None


def test_nssm_exe_in_none_when_absent(tmp_path: Path):
    assert winservice.nssm_exe_in(tmp_path) is None
