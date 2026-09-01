"""Service control is the one Windows-only layer, now split across sc.exe and
systemd. The parsers and command builders are pure, so both platforms' logic is
checked on whatever OS runs the tests."""

import asyncio
import subprocess
import sys
import time
import types

import psutil
import pytest

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
    monkeypatch.setattr(procs, "_find_server", lambda root=None: (proc, False, 1))
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
    monkeypatch.setattr(procs, "_find_server", lambda root=None: (proc, False, 1))
    monkeypatch.setattr(procs.psutil, "cpu_count", lambda: 1)

    assert procs.proc_stats() is not None
    assert proc.cpu_sampled_in_oneshot is False


def test_proc_stats_returns_none_when_server_stopped(monkeypatch):
    monkeypatch.setattr(procs, "_find_server", lambda root=None: (None, False, 0))
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


# ---------------- kill_descendants ----------------


def test_kill_descendants_kills_children_but_not_the_parent():
    """The parent is deliberately spared: whoever launched it (Popen, asyncio)
    has to be the one to reap it, or their wait() reports a bogus result."""
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time;"
         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
         "time.sleep(60)"],
    )
    try:
        # Give the child a moment to spawn its own child.
        deadline = time.time() + 15
        while time.time() < deadline and not psutil.Process(parent.pid).children():
            time.sleep(0.1)
        assert psutil.Process(parent.pid).children(), "grandchild never started"

        assert procs.kill_descendants(parent.pid, timeout=10) is True
        # The grandchild may linger as a zombie (its parent is still hung and
        # hasn't reaped it) — what matters is that it is no longer running, so
        # the pipe it inherited is closed.
        assert all(
            p.status() == psutil.STATUS_ZOMBIE
            for p in psutil.Process(parent.pid).children()
        )
        assert parent.poll() is None, "kill_descendants must leave the parent alone"
    finally:
        parent.kill()
        parent.wait(timeout=10)


def test_kill_descendants_on_a_dead_process_is_success():
    assert procs.kill_descendants(2**31 - 1, timeout=0.1) is True


# ---------------- _wait_for early bail ----------------
#
# A service name the SCM/systemd has never heard of never reaches RUNNING or
# STOPPED. Waiting the full 120s for it just holds the server-operation lock
# while the GUI, dashboard and bot all sit there looking hung, and then returns
# the same False anyway.


def _states(monkeypatch, sequence):
    """Feed service_state a canned sequence (last value repeats)."""
    seen = []

    def fake_state(name):
        seen.append(name)
        return sequence[min(len(seen) - 1, len(sequence) - 1)]

    monkeypatch.setattr(procs, "service_state", fake_state)
    return seen


def test_wait_for_gives_up_early_on_a_service_the_manager_does_not_know(monkeypatch):
    seen = _states(monkeypatch, ["UNKNOWN"])
    start = time.monotonic()
    assert asyncio.run(procs._wait_for("no-such-service", "STOPPED", timeout=120)) is False
    # Bailed on the UNKNOWN streak, nowhere near the 120s timeout.
    assert time.monotonic() - start < 30
    assert len(seen) == procs._UNKNOWN_STREAK_LIMIT


def test_wait_for_tolerates_a_transient_unknown(monkeypatch):
    """One blip (a timed-out sc.exe query) must not be read as 'no such
    service' — the streak has to reset when a real state comes back."""
    _states(monkeypatch, ["UNKNOWN", "UNKNOWN", "STOP_PENDING", "STOPPED"])
    assert asyncio.run(procs._wait_for("palworld", "STOPPED", timeout=120)) is True


def test_wait_for_returns_true_as_soon_as_the_target_is_reached(monkeypatch):
    _states(monkeypatch, ["RUNNING"])
    assert asyncio.run(procs._wait_for("palworld", "RUNNING", timeout=120)) is True


# ---------------- account check: "no mismatch" vs "couldn't tell" ------------
#
# The daemon warns at most once per run, so these two must not be conflated. An
# account split is itself a reason psutil can't read the process, so treating
# "couldn't tell" as "fine" silences the only protection against a blind leak
# watchdog for the life of the daemon — on exactly the boxes that need it.


def test_account_check_is_inconclusive_when_no_process_is_visible(monkeypatch):
    monkeypatch.setattr(procs, "find_process", lambda: None)
    checked, warning = procs.server_account_check("me")
    assert checked is False and warning is None


def test_account_check_is_inconclusive_when_the_owner_cannot_be_read(monkeypatch):
    monkeypatch.setattr(procs, "find_process", lambda: object())
    monkeypatch.setattr(procs, "process_owner", lambda p: None)
    checked, warning = procs.server_account_check("me")
    assert checked is False and warning is None


def test_account_check_is_conclusive_when_the_accounts_match(monkeypatch):
    monkeypatch.setattr(procs, "find_process", lambda: object())
    monkeypatch.setattr(procs, "process_owner", lambda p: "DESKTOP\\me")
    checked, warning = procs.server_account_check("me")
    assert checked is True and warning is None


def test_account_check_is_conclusive_and_warns_on_a_split(monkeypatch):
    monkeypatch.setattr(procs, "find_process", lambda: object())
    monkeypatch.setattr(procs, "process_owner", lambda p: "NT AUTHORITY\\SYSTEM")
    checked, warning = procs.server_account_check("me")
    assert checked is True
    assert warning and "SYSTEM" in warning
# ---------- lingering processes over an install (the silent-failed-update bug) ----------


class _FakeExeProc:
    """Enumerable Palworld process with an exe path that may be unreadable."""

    def __init__(self, exe, pid=1234):
        self.pid = pid
        self.info = {"name": "PalServer-Win64-Shipping.exe"}
        self._exe = exe

    def name(self):
        return "PalServer-Win64-Shipping.exe"

    def exe(self):
        if self._exe is None:
            raise psutil.AccessDenied()
        return self._exe


def test_exe_under_matches_only_paths_inside_the_root():
    root = "/srv/PalServer"
    assert procs._exe_under("/srv/PalServer/Pal/Binaries/Linux/PalServer-Linux-Shipping", root)
    assert not procs._exe_under("/srv/OtherServer/Pal/Binaries/Linux/x", root)
    # The root itself isn't "under" it, and an unreadable exe is never a match.
    assert not procs._exe_under(root, root)
    assert not procs._exe_under(None, root)
    assert not procs._exe_under("/srv/PalServer/x", "")


def test_processes_under_finds_a_server_still_holding_the_install(monkeypatch):
    proc = _FakeExeProc("/srv/PalServer/Pal/Binaries/Linux/PalServer-Linux-Shipping")
    _fake_iter(monkeypatch, [proc])
    assert procs.processes_under("/srv/PalServer") == [proc]
    assert procs.processes_under("/srv/Elsewhere") == []


def test_processes_under_ignores_a_process_it_cannot_attribute(monkeypatch):
    # The cross-account split (server as SYSTEM): the exe path can't be read, so
    # it can't be blamed on this install — blocking updates on it would strand
    # everyone with that setup. The post-update build check covers it instead.
    _fake_iter(monkeypatch, [_FakeExeProc(None)])
    assert procs.processes_under("/srv/PalServer") == []


# ---------- which server gets measured, and how the reading is published ----
#
# Two defects lived here together, and between them they are why the CPU tile
# was wrong twice over: palctl could measure the wrong process, and then divide
# the result into invisibility before anyone saw it.


class _FakePickProc:
    """A candidate for _pick_server: a pid, an image path, and an RSS."""

    def __init__(self, pid, exe="/srv/PalServer/Pal/Binaries/Linux/x", rss=0):
        self.pid = pid
        self._exe = exe
        self._rss = rss

    def exe(self):
        if self._exe is None:
            raise psutil.AccessDenied(self.pid)
        return self._exe

    def memory_info(self):
        return types.SimpleNamespace(rss=self._rss)


def test_two_instances_no_longer_collapse_into_one():
    """They used to be collected into a dict keyed by process *name*, which they
    share — so each later one overwrote the earlier and only one survived to be
    chosen from. psutil enumerates in ascending pid order, making the survivor
    whichever server started LAST, which is precisely the leftover."""
    busy = _FakePickProc(100, "/srv/PalServer/Pal/Binaries/Linux/x", rss=8_000_000_000)
    leftover = _FakePickProc(200, "/srv/Old/Pal/Binaries/Linux/x", rss=50_000_000)
    # No root configured: fall back to size, and the real server is the big one.
    assert procs._pick_server([busy, leftover], None) is busy
    assert procs._pick_server([leftover, busy], None) is busy


def test_the_configured_install_decides_between_two_servers():
    """Size is a heuristic; the install palctl actually manages is a fact. A
    leftover second service is usually a second *install*, so this is exact."""
    managed = _FakePickProc(100, "/srv/PalServer/Pal/Binaries/Linux/x", rss=10)
    leftover = _FakePickProc(200, "/srv/Old/Pal/Binaries/Linux/x", rss=9_000_000_000)
    # ...and it outranks size: the huge one is not ours.
    assert procs._pick_server([managed, leftover], "/srv/PalServer") is managed


def test_an_unattributable_process_still_gets_measured():
    """psutil can't read exe() across an account boundary. Refusing to measure
    anything then would blank the tile on exactly the setups that need it, so an
    install we can't attribute falls back to the size ranking."""
    a = _FakePickProc(100, None, rss=8_000_000_000)
    b = _FakePickProc(200, None, rss=10)
    assert procs._pick_server([a, b], "/srv/PalServer") is a


def test_picking_a_server_is_stable_across_calls():
    """Two identical candidates must not flip between calls — the displayed
    numbers would jump between two processes with nothing changing."""
    a = _FakePickProc(100, "/srv/PalServer/x", rss=1000)
    b = _FakePickProc(200, "/srv/PalServer/x", rss=1000)
    assert {procs._pick_server([a, b], None).pid for _ in range(5)} == {100}


def test_proc_stats_publishes_cores_not_only_a_share_of_the_machine(monkeypatch):
    """The bug behind "the GUI still doesn't report CPU correctly". The sample
    was right; dividing it by the core count and rendering with no decimals threw
    it away. One fully busy core is 1.00 cores on any box — 25% of a 4-core host,
    1.6% of a 64-core one."""
    proc = _FakeMetricsProc(cpu_raw=100.0)  # one core, fully busy
    monkeypatch.setattr(procs, "_find_server", lambda root=None: (proc, False, 1))
    monkeypatch.setattr(procs.psutil, "cpu_count", lambda: 64)

    stats = procs.proc_stats()
    assert stats.cpu_cores == 1.0
    assert stats.cpu_count == 64
    assert round(stats.cpu_percent, 2) == 1.56  # what used to render as "2%"
    assert "1.00 cores" in procs.format_cpu(stats.cpu_cores, stats.cpu_percent)


def test_a_busy_server_never_renders_as_zero_however_many_cores_the_box_has():
    """The literal symptom, pinned across the box sizes people host on."""
    for cores in (4, 8, 16, 32, 64, 128):
        rendered = procs.format_cpu(1.0, 100.0 / cores)  # one core, fully busy
        assert rendered.startswith("1.00 cores"), (cores, rendered)
        assert not rendered.startswith("0"), (cores, rendered)
    # ...and an idle-but-running server stays distinguishable from a dead one.
    assert procs.format_cpu(0.05, 0.08) == "0.05 cores (0.1%)"


def test_the_launcher_is_never_published_as_the_servers_own_load(monkeypatch):
    """The launcher idles at a few MB and ~0% forever. Showing that as the
    server's load is worse than admitting the server couldn't be reached."""
    proc = _FakeMetricsProc(cpu_raw=0.0)
    monkeypatch.setattr(procs, "_find_server", lambda root=None: (proc, True, 1))
    monkeypatch.setattr(procs.psutil, "cpu_count", lambda: 8)

    stats = procs.proc_stats()
    assert stats.measured_launcher is True
    assert "unavailable" in procs.format_cpu(
        stats.cpu_cores, stats.cpu_percent, measured_launcher=True
    )


def test_proc_stats_reports_how_many_servers_are_running(monkeypatch):
    """So a surface can say "2 server instances" rather than quietly describing
    one of them."""
    proc = _FakeMetricsProc(cpu_raw=50.0)
    monkeypatch.setattr(procs, "_find_server", lambda root=None: (proc, False, 2))
    monkeypatch.setattr(procs.psutil, "cpu_count", lambda: 4)
    assert procs.proc_stats().instances == 2


# ---------- against a real process ----------
#
# Every CPU test above this line — and every one that shipped with the two
# previous "CPU reads 0%" fixes — runs against _FakeMetricsProc, whose
# cpu_percent() returns `self._cpu if interval else 0.0`. That fake *models* the
# psutil behaviour the code is trying to get right, so it passes whether or not
# the real thing works, and it cannot see a wrong process being measured at all.
# This one spawns a process that really burns CPU and reads it back through the
# real find_process()/proc_stats(), which is the only way the chain is actually
# under test.


def _spawn_named_burner(tmp_path, name, seconds=8.0):
    """A real process, named so find_process() will match it, burning one core."""
    import shutil as _shutil

    exe = tmp_path / name
    _shutil.copy(sys.executable, exe)
    script = tmp_path / "burn.py"
    script.write_text(
        "import time\n"
        f"end = time.time() + {seconds}\n"
        "while time.time() < end: pass\n",
        encoding="utf-8",
    )
    return subprocess.Popen([str(exe), str(script)])


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="the burner is named for the Linux binary"
)
def test_a_real_busy_server_reads_as_about_one_core(tmp_path):
    """One process pegging one core must read as ~1.00 cores, whatever this
    machine's core count is — the property that makes the number portable, and
    the one a percentage of the machine does not have.

    Deliberately asserted on cores and with a wide band: CI runners are shared
    and oversubscribed, so a tight tolerance here would be a flaky test. A busy
    core reading anywhere near 1 is enough to catch the failures that matter
    (0.0 from a broken sample, or a number scaled by the core count)."""
    proc = _spawn_named_burner(tmp_path, "PalServer-Linux-Shipping")
    try:
        time.sleep(1.0)  # let it get going and become visible to psutil
        found = procs.find_process()
        assert found is not None and found.pid == proc.pid
        stats = procs.proc_stats()
        assert stats is not None and stats.pid == proc.pid
        # Not 0.0 (the symptom of every previous incarnation of this bug)...
        assert stats.cpu_cores > 0.3, stats
        # ...and not scaled by the core count in either direction.
        assert stats.cpu_cores < 2.0, stats
        assert stats.cpu_count == psutil.cpu_count()
        # The rendered string is the actual deliverable.
        assert procs.format_cpu(stats.cpu_cores, stats.cpu_percent).endswith("%)")
        assert not procs.format_cpu(stats.cpu_cores, stats.cpu_percent).startswith("0.0 ")
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="the burner is named for the Linux binary"
)
def test_a_real_leftover_instance_does_not_steal_the_reading(tmp_path):
    """Two real servers, the managed one busy and a leftover idle. Before the
    fix this returned the idle one five times out of five, so the tile read 0%
    while the server pegged a core — and the leak watchdog watched the idle
    instance, which is why it could never fire."""
    managed_root = tmp_path / "managed"
    leftover_root = tmp_path / "leftover"
    managed_root.mkdir()
    leftover_root.mkdir()

    busy = _spawn_named_burner(managed_root, "PalServer-Linux-Shipping")
    idle_exe = leftover_root / "PalServer-Linux-Shipping"
    import shutil as _shutil

    _shutil.copy(sys.executable, idle_exe)
    idle = subprocess.Popen([str(idle_exe), "-c", "import time; time.sleep(8)"])
    try:
        time.sleep(1.0)
        assert len(procs.shipping_processes()) >= 2
        stats = procs.proc_stats(str(managed_root))
        assert stats is not None
        assert stats.pid == busy.pid, "measured the leftover instead of the server"
        assert stats.instances >= 2, "the collision must be reported, not hidden"
        assert stats.cpu_cores > 0.3, stats
    finally:
        for p in (busy, idle):
            p.kill()
            p.wait(timeout=10)
