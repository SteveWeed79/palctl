"""Service control is the one Windows-only layer, now split across sc.exe and
systemd. The parsers and command builders are pure, so both platforms' logic is
checked on whatever OS runs the tests."""

import asyncio

import psutil

from palctl import procs


def test_parse_sc_state():
    assert procs._parse_sc_state("        STATE              : 4  RUNNING") == "RUNNING"
    assert procs._parse_sc_state("        STATE              : 1  STOPPED") == "STOPPED"
    assert procs._parse_sc_state("nope") == "UNKNOWN"


def test_parse_systemctl_state():
    assert procs._parse_systemctl_state("active\n") == "RUNNING"
    assert procs._parse_systemctl_state("inactive") == "STOPPED"
    assert procs._parse_systemctl_state("failed") == "STOPPED"
    assert procs._parse_systemctl_state("activating") == "START_PENDING"
    assert procs._parse_systemctl_state("deactivating") == "STOP_PENDING"
    assert procs._parse_systemctl_state("garbage") == "UNKNOWN"


def test_command_builders_match_platform():
    state = procs._state_command("PalServer")
    start = procs._action_command("start", "PalServer")
    if procs.IS_WINDOWS:
        assert state == ["sc.exe", "query", "PalServer"]
        assert start == ["sc.exe", "start", "PalServer"]
    else:
        assert state == ["systemctl", "is-active", "PalServer"]
        assert start == ["systemctl", "start", "PalServer"]


def test_pal_process_names_cover_both_platforms():
    assert "PalServer-Win64-Shipping.exe" in procs.PAL_PROCESS_NAMES
    assert "PalServer-Linux-Shipping" in procs.PAL_PROCESS_NAMES


# ---------- force-kill escalation primitives ----------


class _FakeProc:
    """Stand-in for psutil.Process. `dies_on` is the weakest signal that kills
    it: 'terminate' dies to a polite terminate(), 'kill' ignores terminate and
    only dies to kill(), 'never' survives both."""

    def __init__(self, dies_on: str = "terminate"):
        self.pid = 1234
        self.signals: list[str] = []
        self._alive = True
        self._dies_on = dies_on

    def terminate(self):
        self.signals.append("terminate")
        if self._dies_on == "terminate":
            self._alive = False

    def kill(self):
        self.signals.append("kill")
        if self._dies_on in ("terminate", "kill"):
            self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise psutil.TimeoutExpired(timeout, self.pid)
        return 0

    def is_running(self):
        return self._alive


def test_terminate_process_reports_a_clean_exit():
    p = _FakeProc(dies_on="terminate")
    assert asyncio.run(procs.terminate_process(p, timeout=0.01)) is True
    assert p.signals == ["terminate"]


def test_terminate_process_reports_a_survivor():
    # Ignores terminate() — the caller needs to know so it can escalate to kill.
    p = _FakeProc(dies_on="kill")
    assert asyncio.run(procs.terminate_process(p, timeout=0.01)) is False


def test_kill_process_hard_stops():
    p = _FakeProc(dies_on="kill")
    assert asyncio.run(procs.kill_process(p, timeout=0.01)) is True
    assert p.signals == ["kill"]


def test_signal_treats_an_already_gone_process_as_success():
    class _Gone:
        pid = 7

        def terminate(self):
            raise psutil.NoSuchProcess(self.pid)

        def kill(self):
            raise psutil.NoSuchProcess(self.pid)

        def wait(self, timeout=None):
            raise psutil.NoSuchProcess(self.pid)

        def is_running(self):
            return False

    assert asyncio.run(procs.terminate_process(_Gone(), timeout=0.01)) is True
