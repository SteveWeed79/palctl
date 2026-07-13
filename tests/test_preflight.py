"""The disk and port checks are what stop a first run from failing halfway, so
their pass/fail boundaries are pinned. The admin/VC++ checks are Windows-only;
here we just confirm they report 'unknown' cleanly off Windows instead of
raising."""

import socket
import sys

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


def test_windows_checks_are_none_off_windows():
    if sys.platform.startswith("win"):
        return  # on Windows these return real True/False; nothing to assert here
    assert preflight.check_admin().ok is None
    assert preflight.check_vcredist().ok is None


def test_check_icon_mapping():
    assert preflight.Check("x", True, "").icon == "✓"
    assert preflight.Check("x", False, "").icon == "❌"
    assert preflight.Check("x", None, "").icon == "⚠️"


def test_run_all_scopes_checks_to_intent(tmp_path):
    # Not installing and not registering services -> only the port check applies.
    checks = preflight.run_all(tmp_path, 8212, need_install=False, need_admin=False)
    assert [c.name for c in checks] == ["Port 8212 free"]

    names = {c.name for c in preflight.run_all(tmp_path, 8212)}
    assert "Disk space" in names and "Visual C++ runtime" in names
