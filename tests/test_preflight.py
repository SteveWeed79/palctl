"""The disk and port checks are what stop a first run from failing halfway, so
their pass/fail boundaries are pinned. The admin/VC++ checks are Windows-only;
here we just confirm they report 'unknown' cleanly off Windows instead of
raising."""

import socket
import sys
import types
from pathlib import Path

import pytest

from palctl import preflight


def test_disk_space_pass_when_need_is_tiny(tmp_path):
    c = preflight.check_disk_space(tmp_path, need_gb=0)
    assert c.ok is True


def test_disk_space_fail_when_need_is_absurd(tmp_path):
    c = preflight.check_disk_space(tmp_path, need_gb=10**9)  # a billion GB
    assert c.ok is False
    assert c.fix  # offers a way out


def test_disk_space_walks_up_to_existing_drive(tmp_path):
    # server_root doesn't exist yet — the check should still work off its drive.
    missing = tmp_path / "does" / "not" / "exist" / "yet"
    c = preflight.check_disk_space(missing, need_gb=0)
    assert c.ok is True


def test_port_free_reports_in_use():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        c = preflight.check_port_free(port)
        assert c.ok is False
        assert str(port) in c.detail
    finally:
        s.close()


def test_port_free_reports_available():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # release it, so the check can bind
    assert preflight.check_port_free(port).ok is True


def test_port_in_use_by_managed_server_is_not_a_conflict(monkeypatch):
    # Adopting palctl onto an already-running server: the server legitimately
    # holds the REST port, so this must be OK, not a red ✗ telling the user to
    # change the port (which would break their working config).
    monkeypatch.setattr(preflight, "_palworld_server_running", lambda: True)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        c = preflight.check_port_free(port)
        assert c.ok is True
        assert "expected" in c.detail.lower()
        assert not c.fix  # no "change the port" advice
    finally:
        s.close()


def test_windows_checks_are_none_off_windows():
    if sys.platform.startswith("win"):
        return  # on Windows these return real True/False; nothing to assert here
    assert preflight.check_admin().ok is None
    assert preflight.check_vcredist().ok is None


def test_is_elevated_none_off_windows():
    # Off Windows (or anywhere ctypes.windll isn't real) it must return None, not
    # False — callers gate on `is False`, so a wrong False would refuse a service
    # install on a platform that doesn't even have the concept.
    if sys.platform.startswith("win"):
        return
    assert preflight.is_elevated() is None


def test_check_icon_mapping():
    assert preflight.Check("x", True, "").icon == "✓"
    assert preflight.Check("x", False, "").icon == "❌"
    assert preflight.Check("x", None, "").icon == "⚠️"


def test_run_all_scopes_checks_to_intent(tmp_path):
    # Not installing and not registering services -> port + instance checks.
    # (The single-server-instance check always applies; without psutil it just
    # reports "couldn't check", but it's still in the list.)
    checks = preflight.run_all(tmp_path, 8212, need_install=False, need_admin=False)
    assert [c.name for c in checks] == ["Port 8212 free", "Single server instance"]

    names = {c.name for c in preflight.run_all(tmp_path, 8212)}
    assert "Disk space" in names and "Visual C++ runtime" in names


def test_single_instance_ok_with_zero_or_one(monkeypatch):
    procs = pytest.importorskip("palctl.procs")
    monkeypatch.setattr(procs, "shipping_processes", lambda: [])
    assert preflight.check_single_server_instance().ok is True

    monkeypatch.setattr(procs, "shipping_processes", lambda: [types.SimpleNamespace(pid=1)])
    assert preflight.check_single_server_instance().ok is True


def test_single_instance_fails_and_names_pids_when_two_running(monkeypatch):
    procs = pytest.importorskip("palctl.procs")
    monkeypatch.setattr(
        procs, "shipping_processes",
        lambda: [types.SimpleNamespace(pid=1000), types.SimpleNamespace(pid=2000)],
    )
    c = preflight.check_single_server_instance()
    assert c.ok is False
    assert "1000" in c.detail and "2000" in c.detail  # points at the culprits
    assert c.fix  # tells them to disable the extra service


def test_vcredist_signature_gate_fails_closed_on_tamper():
    # The bytes don't match a signature that should be there -> refuse to run it.
    assert preflight._signature_is_tampered("HashMismatch") is True
    assert preflight._signature_is_tampered("NotSigned") is True
    assert preflight._signature_is_tampered("notsigned") is True  # case-insensitive


def test_vcredist_signature_gate_fails_open_on_unknown():
    # A machine that just can't verify (missing certs -> NotTrusted, no
    # PowerShell -> "") must still be able to install the runtime it needs.
    for status in ("Valid", "", "NotTrusted", "UnknownError"):
        assert preflight._signature_is_tampered(status) is False


def test_authenticode_status_is_empty_off_windows():
    if sys.platform.startswith("win"):
        return  # on Windows it shells out to PowerShell; nothing to assert here
    assert preflight._authenticode_status(Path("whatever.exe")) == ""
