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


def test_config_xml_stop_timeout_default_and_override():
    assert "<stoptimeout>30 sec</stoptimeout>" in winservice.winsw_config_xml("s", "s.exe")
    assert "<stoptimeout>90 sec</stoptimeout>" in winservice.winsw_config_xml(
        "s", "s.exe", stop_timeout="90 sec"
    )


def test_install_service_scrubs_the_password_after_registration(monkeypatch, tmp_path):
    # The SCM stores the account password itself (encrypted) once `install`
    # runs — the XML copy must not outlive that moment. Leaving it would keep a
    # Windows password in a plaintext file for the service's lifetime (NSSM
    # never had this trap; it stored nothing).
    calls, winsw = _install_env(monkeypatch, tmp_path, exists=False)

    winservice.install_service(
        winsw, "palctl-daemon", "svc.exe", user=r".\steve", password="hunter2",
        appdata=r"C:\Users\steve\AppData\Roaming",
    )

    _, svc_xml = winservice.wrapper_paths(tmp_path, "palctl-daemon")
    text = svc_xml.read_text(encoding="utf-8")
    assert "hunter2" not in text                      # the secret is gone
    assert r"<username>.\steve</username>" in text    # the account remains
    assert "<allowservicelogon>true</allowservicelogon>" in text
    # The APPDATA redirect must survive the scrub — losing it here would
    # re-create the split-config 401 on the next service start.
    assert 'name="APPDATA"' in text
    # And registration really happened before the scrub (install was issued).
    svc_exe, _ = winservice.wrapper_paths(tmp_path, "palctl-daemon")
    assert [str(svc_exe), "install"] in calls


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


def test_config_xml_user_service_still_gets_appdata_redirect():
    # A user-account service needs the redirect TOO: the SCM builds a service's
    # environment from the SYSTEM block — %APPDATA% is an interactive-shell
    # variable, absent even when the service logs on as that user. Without the
    # env line the daemon falls into config_dir()'s ~/.config fallback and
    # reads a different token than the GUI → 401 on every call. (This test
    # used to pin the opposite — "user wins over the redirect" — which is
    # exactly the bug that shipped.)
    xml = winservice.winsw_config_xml(
        "svc", "svc.exe",
        user=r".\steve", password="pw", appdata=r"C:\Users\steve\AppData\Roaming",
    )
    assert "<serviceaccount>" in xml
    assert r'<env name="APPDATA" value="C:\Users\steve\AppData\Roaming"/>' in xml


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


def _fake_frozen(monkeypatch, exe_dir: Path):
    """Pretend we're the frozen build, with palctl's exe living in exe_dir."""
    monkeypatch.setattr(winservice.sys, "frozen", True, raising=False)
    monkeypatch.setattr(winservice.sys, "executable", str(exe_dir / "palctl-gui.exe"))


def test_ensure_winsw_discards_a_tampered_cache(tmp_path: Path, monkeypatch):
    # The cache becomes a SYSTEM service binary — it is verified on every use,
    # not just at download time. Tampered bytes are discarded and replaced via
    # the verified paths; a manual drop that MATCHES the pin still works.
    data = b"MZ-good-winsw"
    good = hashlib.sha256(data).hexdigest()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "winsw.exe").write_bytes(b"MZ-tampered")
    _fake_download(monkeypatch, data)

    out = winservice.ensure_winsw(cache, sha256=good)
    assert out.read_bytes() == data  # replaced, not trusted


def test_ensure_winsw_prefers_the_bundled_copy_over_downloading(
    tmp_path: Path, monkeypatch
):
    # The installer ships winsw.exe next to palctl's exes; a frozen build must
    # use it and never touch the network — that download is where fresh boxes
    # (sparse cert store) and AV HTTPS-scanning used to kill setup.
    data = b"MZ-bundled-winsw"
    good = hashlib.sha256(data).hexdigest()
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    (exe_dir / "winsw.exe").write_bytes(data)
    _fake_frozen(monkeypatch, exe_dir)
    monkeypatch.setattr(
        "palctl.fetch.open_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")),
    )

    out = winservice.ensure_winsw(tmp_path / "cache", sha256=good)
    assert out == tmp_path / "cache" / "winsw.exe"
    assert out.read_bytes() == data


def test_bundled_winsw_rejects_a_tampered_copy_and_none_when_not_frozen(
    tmp_path: Path, monkeypatch
):
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    (exe_dir / "winsw.exe").write_bytes(b"MZ-tampered")
    # Not frozen (dev checkout): no bundled copy, regardless of what's on disk.
    monkeypatch.setattr(winservice.sys, "frozen", False, raising=False)
    assert winservice.bundled_winsw(sha256="f" * 64) is None
    # Frozen but the bytes don't match the pin: skipped, never used.
    _fake_frozen(monkeypatch, exe_dir)
    assert winservice.bundled_winsw(sha256="f" * 64) is None
    # Frozen and matching: returned.
    good = hashlib.sha256(b"MZ-tampered").hexdigest()
    assert winservice.bundled_winsw(sha256=good) == exe_dir / "winsw.exe"


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
