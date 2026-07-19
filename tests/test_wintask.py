"""The scheduled health task — Windows hung-daemon recovery. Builders are pure
and pinned here; the runners are exercised against a faked schtasks."""

import subprocess

from palctl import wintask


def test_create_task_command_shape():
    cmd = wintask.create_task_command(r"C:\app\palctl-daemon.exe", every_minutes=5)
    assert cmd[:3] == ["schtasks", "/Create", "/F"]  # /F: reinstall converges
    assert wintask.TASK_NAME in cmd
    tr = cmd[cmd.index("/TR") + 1]
    assert tr == r'"C:\app\palctl-daemon.exe" health-check'  # quoted: path has spaces sometimes
    assert cmd[cmd.index("/MO") + 1] == "5"
    assert "/RU" not in cmd  # user task by default (login mode)


def test_create_task_command_as_system_and_args():
    cmd = wintask.create_task_command(
        "python", "-m palctl.daemon", every_minutes=0, as_system=True
    )
    tr = cmd[cmd.index("/TR") + 1]
    assert tr == '"python" -m palctl.daemon health-check'  # dev checkout works too
    assert cmd[cmd.index("/MO") + 1] == "1"  # clamped: 0 would be rejected by schtasks
    assert cmd[cmd.index("/RU") + 1] == "SYSTEM"
    assert cmd[cmd.index("/RL") + 1] == "HIGHEST"


def test_delete_and_query_commands():
    assert wintask.delete_task_command() == [
        "schtasks", "/Delete", "/F", "/TN", wintask.TASK_NAME
    ]
    assert wintask.query_task_command() == [
        "schtasks", "/Query", "/TN", wintask.TASK_NAME
    ]


def _fake_run(monkeypatch, codes: dict[str, int]):
    """schtasks stub: returncode chosen by the verb (/Create, /Query, /Delete)."""
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, codes.get(cmd[1], 0), "", "")

    monkeypatch.setattr(wintask, "_run", run)
    monkeypatch.setattr(wintask, "_on_windows", lambda: True)
    return calls


def test_register_health_task_true_on_success(monkeypatch):
    calls = _fake_run(monkeypatch, {"/Create": 0})
    assert wintask.register_health_task("exe") is True
    assert calls[0][1] == "/Create"


def test_register_health_task_false_on_refusal_and_off_windows(monkeypatch):
    _fake_run(monkeypatch, {"/Create": 1})
    assert wintask.register_health_task("exe") is False
    monkeypatch.setattr(wintask, "_on_windows", lambda: False)
    assert wintask.register_health_task("exe") is False


def test_remove_health_task_absent_counts_as_removed(monkeypatch):
    # /Query nonzero = not registered; nothing to delete, and that's success.
    calls = _fake_run(monkeypatch, {"/Query": 1})
    assert wintask.remove_health_task() is True
    assert all(c[1] != "/Delete" for c in calls)


def test_remove_health_task_deletes_when_present(monkeypatch):
    calls = _fake_run(monkeypatch, {"/Query": 0, "/Delete": 0})
    assert wintask.remove_health_task() is True
    assert calls[-1][1] == "/Delete"
