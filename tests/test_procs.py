"""Service control is the one Windows-only layer, now split across sc.exe and
systemd. The parsers and command builders are pure, so both platforms' logic is
checked on whatever OS runs the tests."""

import asyncio
import types

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


# ---------- process metrics (the always-0 CPU bug) ----------


class _FakeMetricsProc:
    """psutil.Process stand-in for proc_stats.

    cpu_percent here models an interval measurement: given a window (interval>0)
    it returns a real reading straight away, and — unlike the interval=None API —
    it never depends on a prior "priming" call, which is the whole point. It also
    records whether it was asked to sample with a real window, and whether that
    happened before oneshot() was entered, so the tests can pin down the two ways
    the old code read 0.0."""

    def __init__(self, pid=999, cpu_raw=80.0):
        self.pid = pid
        self._cpu = cpu_raw
        self.cpu_interval = None
        self.in_oneshot = False
        self.cpu_sampled_in_oneshot = None

    def oneshot(self):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            self.in_oneshot = True
            try:
                yield
            finally:
                self.in_oneshot = False

        return _cm()

    def memory_info(self):
        return types.SimpleNamespace(rss=1_048_576 * 100)  # 100 MB

    def cpu_percent(self, interval=None):
        self.cpu_interval = interval
        self.cpu_sampled_in_oneshot = self.in_oneshot
        # A real window yields a real number; without one, mimic psutil's 0.0.
        return self._cpu if interval else 0.0

    def num_threads(self):
        return 12

    def create_time(self):
        return 0.0


def test_proc_stats_reports_cpu_on_the_very_first_read(monkeypatch):
    # The always-0 bug: a caller that reads once (the bot's /status, `palctl
    # status` right after start) must still get a real CPU number. proc_stats
    # samples over a real window, so even a brand-new process object reads > 0.
    proc = _FakeMetricsProc(cpu_raw=800.0)  # raw per-core sum
    monkeypatch.setattr(procs, "find_process", lambda: proc)
    monkeypatch.setattr(procs.psutil, "cpu_count", lambda: 8)

    stats = procs.proc_stats()
    assert stats is not None
    assert proc.cpu_interval == procs._CPU_SAMPLE_SECONDS  # measured over a window
    assert stats.cpu_percent == 100.0  # 800% across 8 cores, normalized


def test_proc_stats_samples_cpu_outside_oneshot(monkeypatch):
    # oneshot() caches cpu_times(), so an interval sample taken inside it diffs a
    # value against itself and reads 0.0. The CPU sample must happen before the
    # oneshot block, or the bug comes straight back.
    proc = _FakeMetricsProc(cpu_raw=100.0)
    monkeypatch.setattr(procs, "find_process", lambda: proc)
    monkeypatch.setattr(procs.psutil, "cpu_count", lambda: 1)

    assert procs.proc_stats() is not None
    assert proc.cpu_sampled_in_oneshot is False


def test_proc_stats_returns_none_when_server_stopped(monkeypatch):
    monkeypatch.setattr(procs, "find_process", lambda: None)
    assert procs.proc_stats() is None


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
