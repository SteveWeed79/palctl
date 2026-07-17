"""NSSM registration is Windows-only, but the command sequence and the archive
layout logic are the parts that go wrong silently, so those are pinned here."""

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from palctl import winservice


def test_install_commands_full():
    cmds = winservice.install_commands(
        "nssm.exe", "palctl-daemon", r"C:\app\palctl-daemon.exe",
        args="-m palctl.daemon", app_dir=r"C:\app",
    )
    assert cmds[0] == ["nssm.exe", "install", "palctl-daemon", r"C:\app\palctl-daemon.exe"]
    joined = [" ".join(c) for c in cmds]
    # Application is set explicitly so a re-install can repair a wrong exe path.
    assert ["nssm.exe", "set", "palctl-daemon", "Application", r"C:\app\palctl-daemon.exe"] in cmds
    assert any("AppParameters" in j and "palctl.daemon" in j for j in joined)
    assert any("AppDirectory" in j for j in joined)
    # Auto-start is always configured, and last.
    assert cmds[-1] == ["nssm.exe", "set", "palctl-daemon", "Start", "SERVICE_AUTO_START"]


def test_install_commands_minimal_still_sets_autostart():
    cmds = winservice.install_commands("nssm.exe", "svc", "svc.exe")
    assert cmds[0] == ["nssm.exe", "install", "svc", "svc.exe"]
    assert ["nssm.exe", "set", "svc", "Application", "svc.exe"] in cmds
    assert ["nssm.exe", "set", "svc", "Start", "SERVICE_AUTO_START"] in cmds
    # No args and no app_dir -> install + the Application set + the autostart set.
    assert len(cmds) == 3


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


def test_install_commands_as_user_sets_objectname():
    cmds = winservice.install_commands(
        "nssm.exe", "svc", "svc.exe", user=r".\steve", password="hunter2",
    )
    assert ["nssm.exe", "set", "svc", "ObjectName", r".\steve", "hunter2"] in cmds


def test_install_commands_localsystem_redirects_appdata():
    # Without a user account, the service stays LocalSystem — whose %APPDATA%
    # is NOT the installing user's. The redirect keeps daemon and GUI reading
    # the same config, token, and logs.
    cmds = winservice.install_commands(
        "nssm.exe", "svc", "svc.exe", appdata=r"C:\Users\steve\AppData\Roaming",
    )
    assert [
        "nssm.exe", "set", "svc", "AppEnvironmentExtra",
        r"APPDATA=C:\Users\steve\AppData\Roaming",
    ] in cmds


def test_install_commands_user_wins_over_appdata_redirect():
    # Running AS the user makes the redirect pointless — never set both.
    cmds = winservice.install_commands(
        "nssm.exe", "svc", "svc.exe",
        user=r".\steve", password="pw", appdata=r"C:\Users\steve\AppData\Roaming",
    )
    joined = [" ".join(c) for c in cmds]
    assert any("ObjectName" in j for j in joined)
    assert not any("AppEnvironmentExtra" in j for j in joined)


def test_install_service_stops_before_start_to_reload_the_daemon(monkeypatch):
    # A re-install over a RUNNING daemon must stop it before starting, or the
    # live process keeps the pre-`set` exe/params (`nssm start` no-ops when the
    # service is already running).
    calls: list[list[str]] = []
    monkeypatch.setattr(winservice, "_run", lambda cmd: calls.append(cmd))

    winservice.install_service("nssm.exe", "palctl-daemon", "svc.exe")

    stop = ["nssm.exe", "stop", "palctl-daemon"]
    start = ["nssm.exe", "start", "palctl-daemon"]
    assert stop in calls and start in calls
    assert calls.index(stop) < calls.index(start)


def test_install_service_start_false_skips_stop_and_start(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(winservice, "_run", lambda cmd: calls.append(cmd))

    winservice.install_service("nssm.exe", "svc", "svc.exe", start=False)

    assert not any(c[1:2] == ["stop"] for c in calls)
    assert not any(c[1:2] == ["start"] for c in calls)


# ---------- NSSM download checksum pin ----------


def _nssm_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nssm-2.24/win32/nssm.exe", b"MZ-fake-32")
        z.writestr("nssm-2.24/win64/nssm.exe", b"MZ-fake-64")
    return buf.getvalue()


def _fake_download(monkeypatch, data: bytes):
    monkeypatch.setattr(
        winservice.urllib.request, "urlopen",
        lambda url, timeout=None: io.BytesIO(data),
    )


def test_pinned_nssm_sha256_is_well_formed():
    # A typo in the pin would refuse every real download — guard the literal.
    assert len(winservice.NSSM_SHA256) == 64
    int(winservice.NSSM_SHA256, 16)  # all hex


def test_ensure_nssm_unpacks_a_matching_download(tmp_path: Path, monkeypatch):
    data = _nssm_zip_bytes()
    _fake_download(monkeypatch, data)
    good = hashlib.sha256(data).hexdigest()
    out = winservice.ensure_nssm(tmp_path / "cache", sha256=good)
    assert out.exists() and out.name == "nssm.exe"
    assert out.read_bytes() == b"MZ-fake-64"  # win64 preferred


def test_ensure_nssm_refuses_a_tampered_download(tmp_path: Path, monkeypatch):
    _fake_download(monkeypatch, _nssm_zip_bytes())
    cache = tmp_path / "cache"
    with pytest.raises(winservice.NssmChecksumError):
        winservice.ensure_nssm(cache, sha256="0" * 64)
    # Nothing unverified was left on disk as the usable binary.
    assert not (cache / "nssm.exe").exists()
