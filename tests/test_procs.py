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


def test_parse_sc_exit_code():
    out = (
        "        STATE              : 1  STOPPED\n"
        "        WIN32_EXIT_CODE    : 1069  (0x42d)\n"
        "        SERVICE_EXIT_CODE  : 0  (0x0)\n"
    )
    assert procs._parse_sc_exit_code(out) == 1069
    # A clean service reports 0 — that's "nothing wrong", not a missing code.
    assert procs._parse_sc_exit_code("WIN32_EXIT_CODE    : 0  (0x0)") == 0
    # SERVICE_EXIT_CODE must not be mistaken for the WIN32 one.
    assert procs._parse_sc_exit_code("SERVICE_EXIT_CODE  : 42  (0x2a)") is None
    assert procs._parse_sc_exit_code("no code here") is None


def test_service_failure_reason_explains_known_codes(monkeypatch):
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(
        procs, "_run_capture",
        lambda cmd, timeout=30.0: "WIN32_EXIT_CODE    : 1069  (0x42d)",
    )
    reason = procs.service_failure_reason("palctl-daemon")
    assert reason and "1069" in reason and "login startup" in reason


def test_service_failure_reason_unknown_code_is_still_reported(monkeypatch):
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(
        procs, "_run_capture", lambda cmd, timeout=30.0: "WIN32_EXIT_CODE : 999 (0x3e7)"
    )
    assert "999" in procs.service_failure_reason("svc")


def test_service_failure_reason_none_when_clean_or_off_windows(monkeypatch):
    # A healthy service (code 0) must not invent a problem.
    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    monkeypatch.setattr(procs, "_run_capture", lambda cmd, timeout=30.0: "WIN32_EXIT_CODE : 0")
    assert procs.service_failure_reason("svc") is None
    # Off Windows there's no such code at all.
    monkeypatch.setattr(procs, "IS_WINDOWS", False)
    assert procs.service_failure_reason("svc") is None


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


# ---------- find_process: watch the real server, not the launcher ----------


class _FakeEnumProc:
    """Stand-in for psutil.Process during enumeration. `name=None` models a name
    psutil couldn't read across a privilege boundary."""

    def __init__(self, name, *, children=None, owner=None):
        self._name = name
        self.info = {"name": name}
        self._children = children or []
        self._owner = owner

    def name(self):
        if self._name is None:
            raise psutil.AccessDenied()
        return self._name

    def children(self):
        return self._children

    def username(self):
        if self._owner is None:
            raise psutil.AccessDenied()
        return self._owner


def _fake_iter(monkeypatch, procs_list):
    monkeypatch.setattr(procs.psutil, "process_iter", lambda attrs=None: procs_list)


def test_find_process_prefers_shipping_seen_directly(monkeypatch):
    shipping = _FakeEnumProc("PalServer-Win64-Shipping.exe")
    launcher = _FakeEnumProc("PalServer.exe", children=[shipping])
    _fake_iter(monkeypatch, [launcher, shipping])
    assert procs.find_process() is shipping  # the real server, not the launcher


def test_find_process_follows_launcher_to_named_child(monkeypatch):
    # The server runs as SYSTEM: its name wasn't enumerable (info name None), so
    # it's not a top-level candidate — but the launcher's child IS it.
    child = _FakeEnumProc("PalServer-Win64-Shipping.exe")
    launcher = _FakeEnumProc("PalServer.exe", children=[child])
    unnamed_top = _FakeEnumProc(None)  # the same server, name unreadable up top
    _fake_iter(monkeypatch, [launcher, unnamed_top])
    assert procs.find_process() is child


def test_find_process_follows_launcher_to_sole_unnamed_child(monkeypatch):
    # Even if the child's name can't be read, a launcher with exactly one child
    # IS the server — never settle for the idle launcher.
    child = _FakeEnumProc(None)
    launcher = _FakeEnumProc("PalServer.exe", children=[child])
    _fake_iter(monkeypatch, [launcher])
    assert procs.find_process() is child


def test_find_process_none_when_nothing_running(monkeypatch):
    _fake_iter(monkeypatch, [_FakeEnumProc("explorer.exe")])
    assert procs.find_process() is None


# ---------- account-split detection (the watchdog-blinding bug) ----------


def test_account_mismatch_warning_flags_system_vs_user():
    w = procs.account_mismatch_warning("NT AUTHORITY\\SYSTEM", "DESKTOP\\server sw")
    assert w and "SYSTEM" in w and "install-service --as-user" in w


def test_account_mismatch_warning_silent_when_same_account():
    # Same trailing account name (domain prefix differs) → no warning.
    assert procs.account_mismatch_warning("DESKTOP\\server sw", "server sw") is None
    # Unknown owner → nothing to say.
    assert procs.account_mismatch_warning(None, "server sw") is None


def test_process_owner_reads_username(monkeypatch):
    assert procs.process_owner(_FakeEnumProc("x", owner="NT AUTHORITY\\SYSTEM")) == (
        "NT AUTHORITY\\SYSTEM"
    )
    assert procs.process_owner(_FakeEnumProc("x", owner=None)) is None  # AccessDenied


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
