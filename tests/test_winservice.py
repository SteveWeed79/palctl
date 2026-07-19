"""WinSW registration is Windows-only, but the config XML and the command
sequencing are the parts that go wrong silently, so those are pinned here."""

import hashlib
import io
from pathlib import Path

import pytest

from palctl import winservice

# ---------- the declarative service definition ----------


def test_config_xml_full():
    xml = winservice.winsw_config_xml(
        "palctl-daemon", r"C:\app\palctl-daemon.exe",
        args="-m palctl.daemon", app_dir=r"C:\app",
    )
    assert "<id>palctl-daemon</id>" in xml
    assert r"<executable>C:\app\palctl-daemon.exe</executable>" in xml
    assert "<arguments>-m palctl.daemon</arguments>" in xml
    assert r"<workingdirectory>C:\app</workingdirectory>" in xml
    # Auto-start and keep-alive are always configured — the whole point of a
    # service (parity with the systemd unit's Restart=on-failure).
    assert "<startmode>Automatic</startmode>" in xml
    assert '<onfailure action="restart"' in xml


def test_config_xml_minimal_omits_optional_fields():
    xml = winservice.winsw_config_xml("svc", "svc.exe")
    assert "<arguments>" not in xml
    assert "<workingdirectory>" not in xml
    assert "<serviceaccount>" not in xml
    assert "APPDATA" not in xml
    assert "<description>svc</description>" in xml  # falls back to the name


def test_config_xml_as_user_sets_serviceaccount_and_logon_right():
    xml = winservice.winsw_config_xml(
        "svc", "svc.exe", user=r".\steve", password="hunter2",
    )
    assert r"<username>.\steve</username>" in xml
    assert "<password>hunter2</password>" in xml
    # WinSW grants "Log on as a service" itself — one less 1069 cause.
    assert "<allowservicelogon>true</allowservicelogon>" in xml


def test_config_xml_localsystem_redirects_appdata():
    # Without a user account, the service stays LocalSystem — whose %APPDATA%
    # is NOT the installing user's. The redirect keeps daemon and GUI reading
    # the same config, token, and logs.
    xml = winservice.winsw_config_xml(
        "svc", "svc.exe", appdata=r"C:\Users\steve\AppData\Roaming",
    )
    assert r'<env name="APPDATA" value="C:\Users\steve\AppData\Roaming"/>' in xml


def test_config_xml_user_wins_over_appdata_redirect():
    # Running AS the user makes the redirect pointless — never set both.
    xml = winservice.winsw_config_xml(
        "svc", "svc.exe",
        user=r".\steve", password="pw", appdata=r"C:\Users\steve\AppData\Roaming",
    )
    assert "<serviceaccount>" in xml
    assert "APPDATA" not in xml


def test_config_xml_escapes_markup_in_values():
    # A password (or path) containing XML metacharacters must not corrupt the
    # config — a service silently registered with the wrong password is the
    # 1069 bug with extra steps.
    xml = winservice.winsw_config_xml(
        "svc", "svc.exe", user=r".\s", password='p<&>"w',
    )
    assert "<password>p&lt;&amp;&gt;\"w</password>" in xml


def test_wrapper_paths_pair_by_basename(tmp_path: Path):
    exe, xml = winservice.wrapper_paths(tmp_path, "palctl-daemon")
    # WinSW v2 finds its config by the exe's basename — they must match.
    assert exe.stem == xml.stem
    assert exe.suffix == ".exe" and xml.suffix == ".xml"
    assert exe.parent == tmp_path


# ---------- install/remove sequencing ----------


def _install_env(monkeypatch, tmp_path: Path, *, exists: bool):
    """A fake SCM: records commands, flips service existence on `sc delete`."""
    calls: list[list[str]] = []
    state = {"exists": exists}

    def run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["sc.exe", "delete"]:
            state["exists"] = False

    monkeypatch.setattr(winservice, "_run", run)
    monkeypatch.setattr(winservice, "service_exists", lambda name: state["exists"])
    monkeypatch.setattr("palctl.procs.service_state", lambda name: "STOPPED")
    winsw = tmp_path / "winsw.exe"
    winsw.write_bytes(b"MZ-winsw")
    return calls, winsw


def test_install_service_replaces_an_existing_registration(monkeypatch, tmp_path):
    # A re-install must be stop → delete → register → start, and the config is
    # rewritten whole — so nothing stale (old args, old account) can survive
    # from the previous registration, by construction.
    calls, winsw = _install_env(monkeypatch, tmp_path, exists=True)

    winservice.install_service(
        winsw, "palctl-daemon", "svc.exe", args="-m palctl.daemon",
    )

    svc_exe, svc_xml = winservice.wrapper_paths(tmp_path, "palctl-daemon")
    stop = ["sc.exe", "stop", "palctl-daemon"]
    delete = ["sc.exe", "delete", "palctl-daemon"]
    register = [str(svc_exe), "install"]
    start = [str(svc_exe), "start"]
    assert (
        calls.index(stop)
        < calls.index(delete)
        < calls.index(register)
        < calls.index(start)
    )
    # The wrapper copy and the whole-truth config were (re)written.
    assert svc_exe.read_bytes() == b"MZ-winsw"
    assert "<arguments>-m palctl.daemon</arguments>" in svc_xml.read_text()


def test_install_service_raises_when_old_registration_wont_die(monkeypatch, tmp_path):
    # The SCM keeps a removed service "pending deletion" while anything holds a
    # handle to it, and re-creating the name then fails. Surface that with the
    # cause instead of silently configuring a zombie registration.
    calls: list[list[str]] = []
    monkeypatch.setattr(winservice, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr("palctl.procs.service_state", lambda name: "STOPPED")
    monkeypatch.setattr(  # single-shot wait so the test doesn't sit out the timeout
        winservice, "_wait_for", lambda pred, timeout, interval=1.0: pred()
    )
    winsw = tmp_path / "winsw.exe"
    winsw.write_bytes(b"MZ-winsw")

    with pytest.raises(RuntimeError, match="pending deletion"):
        winservice.install_service(winsw, "svc", "svc.exe")


def test_install_service_fresh_registration_skips_removal(monkeypatch, tmp_path):
    calls, winsw = _install_env(monkeypatch, tmp_path, exists=False)

    winservice.install_service(winsw, "svc", "svc.exe")

    assert not any(c[:2] == ["sc.exe", "stop"] for c in calls)
    assert not any(c[:2] == ["sc.exe", "delete"] for c in calls)
    svc_exe, _ = winservice.wrapper_paths(tmp_path, "svc")
    assert [str(svc_exe), "start"] in calls


def test_install_service_start_false_skips_start(monkeypatch, tmp_path):
    calls, winsw = _install_env(monkeypatch, tmp_path, exists=False)

    winservice.install_service(winsw, "svc", "svc.exe", start=False)

    assert not any(c[1:2] == ["start"] for c in calls)


def test_start_service_uses_plain_scm(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(winservice, "_run", lambda cmd: calls.append(cmd))
    winservice.start_service("svc")
    assert calls == [["sc.exe", "start", "svc"]]


def test_remove_service_stops_waits_then_deletes(monkeypatch):
    # sc.exe works on ANY service — including NSSM-era registrations — so
    # upgrades migrate without the old wrapper. The wait between stop and
    # delete is what prevents the "pending deletion" zombie.
    calls: list[list[str]] = []
    monkeypatch.setattr(winservice, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("palctl.procs.service_state", lambda name: "STOPPED")

    winservice.remove_service("svc")

    assert calls == [["sc.exe", "stop", "svc"], ["sc.exe", "delete", "svc"]]


def test_wait_for_polls_until_true(monkeypatch):
    monkeypatch.setattr(winservice.time, "sleep", lambda s: None)
    vals = iter([False, False, True])
    assert winservice._wait_for(lambda: next(vals), timeout=60.0) is True


def test_wait_for_times_out(monkeypatch):
    monkeypatch.setattr(winservice.time, "sleep", lambda s: None)
    assert winservice._wait_for(lambda: False, timeout=0.0) is False


# ---------- WinSW download checksum pin ----------


def _fake_download(monkeypatch, data: bytes):
    # ensure_winsw downloads via fetch.open_url (system trust + certifi retry).
    monkeypatch.setattr(
        "palctl.fetch.open_url",
        lambda url, timeout=None: io.BytesIO(data),
    )


def test_pinned_winsw_sha256_is_well_formed():
    # A typo in the pin would refuse every real download — guard the literal.
    assert len(winservice.WINSW_SHA256) == 64
    int(winservice.WINSW_SHA256, 16)  # all hex


def test_ensure_winsw_caches_a_matching_download(tmp_path: Path, monkeypatch):
    data = b"MZ-fake-winsw"
    _fake_download(monkeypatch, data)
    good = hashlib.sha256(data).hexdigest()
    out = winservice.ensure_winsw(tmp_path / "cache", sha256=good)
    assert out.exists() and out.name == "winsw.exe"
    assert out.read_bytes() == data
    # Second call reuses the cache — no download.
    monkeypatch.setattr(
        "palctl.fetch.open_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-downloaded")),
    )
    assert winservice.ensure_winsw(tmp_path / "cache", sha256=good) == out


def test_ensure_winsw_blocked_download_names_the_manual_workaround(
    tmp_path: Path, monkeypatch
):
    # A box where HTTPS verification fails (AV scanning, broken cert store)
    # must get an actionable escape hatch — the exact file to download and
    # where to put it — not a bare _ssl.c error that stops setup dead.
    monkeypatch.setattr(
        "palctl.fetch.open_url",
        lambda *a, **k: (_ for _ in ()).throw(OSError("could not verify the HTTPS")),
    )
    cache = tmp_path / "cache"
    with pytest.raises(OSError, match="Workaround") as ei:
        winservice.ensure_winsw(cache)
    assert str(cache / "winsw.exe") in str(ei.value)  # tells them where to put it
    assert winservice.WINSW_SHA256 in str(ei.value)   # and how to check it


def test_ensure_winsw_refuses_a_tampered_download(tmp_path: Path, monkeypatch):
    _fake_download(monkeypatch, b"MZ-fake-winsw")
    cache = tmp_path / "cache"
    with pytest.raises(winservice.WrapperChecksumError):
        winservice.ensure_winsw(cache, sha256="f" * 64)
    # Nothing unverified was left on disk as the usable binary.
    assert not (cache / "winsw.exe").exists()
