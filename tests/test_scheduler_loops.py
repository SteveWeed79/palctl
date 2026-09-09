"""The scheduler's loops as a set, and the update check as a habit.

Three things here only go wrong over time, which is why none of them had a
test: a crashed loop leaving its siblings behind to be duplicated, a failing
backup retried every five minutes for good, and an "update available" that
repeated on every check. The fourth — the check running SteamCMD beside an
update already running SteamCMD — only goes wrong when two clocks line up.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from palctl import backups
from palctl import scheduler as sched_mod
from palctl.config import Config
from palctl.events import Event, EventBus
from palctl.scheduler import _BACKUP_OVERDUE_GRACE, Scheduler, update_check_seconds


class FakeApi:
    async def save(self):
        pass

    async def wait_until_alive(self, timeout=240):
        return True

    async def announce(self, message):
        pass

    async def players(self):
        return []


def _collect(bus: EventBus) -> list[Event]:
    seen: list[Event] = []

    async def handler(e: Event) -> None:
        seen.append(e)

    bus.on_any(handler)
    return seen


def _sched(tmp_path: Path, **kw) -> Scheduler:
    cfg = Config()
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg.steamcmd_path = str(steam)
    return Scheduler(cfg, FakeApi(), EventBus(), **kw)


# ---------------- run(): one crashed loop must not duplicate the others ----


def test_a_crashed_loop_takes_its_siblings_down_with_it(tmp_path, monkeypatch):
    """gather() propagated the first exception and left the other three loops
    running. The daemon's supervisor then restarted run() — four more loops
    beside the three survivors: two backup loops, two autosave loops, and a
    third set after the next crash."""
    s = _sched(tmp_path)
    survived = {"backup": False, "autosave": False, "update": False}

    def forever(name):
        async def loop():
            try:
                await asyncio.sleep(3600)
            finally:
                survived[name] = False  # cancelled: good
            survived[name] = True  # only reached if it ran to completion — never

        return loop

    async def crashing_restart_loop():
        await asyncio.sleep(0)
        raise TypeError("'<' not supported between instances of 'str' and 'int'")

    monkeypatch.setattr(s, "_backup_loop", forever("backup"))
    monkeypatch.setattr(s, "_autosave_loop", forever("autosave"))
    monkeypatch.setattr(s, "_auto_update_loop", forever("update"))
    monkeypatch.setattr(s, "_daily_restart_loop", crashing_restart_loop)

    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — an ExceptionGroup
        asyncio.run(asyncio.wait_for(s.run(), timeout=5))
    # The crash still surfaces to the supervisor (as an ExceptionGroup, which
    # is an Exception, so the supervisor's `except Exception` catches it)...
    assert isinstance(excinfo.value, Exception)
    assert "TypeError" in repr(excinfo.value) or "str" in str(excinfo.value)
    # ...and nothing survived to be duplicated by the restart.
    assert not any(survived.values())


# ---------------- the backup loop after a failure ----------------


def test_a_failing_backup_is_retried_with_backoff_not_every_five_minutes():
    hours = 6
    assert Scheduler._backup_retry_wait(0, hours) == 0.0
    waits = [Scheduler._backup_retry_wait(n, hours) for n in range(1, 8)]
    assert waits[0] == _BACKUP_OVERDUE_GRACE * 2  # ten minutes, not five
    assert waits == sorted(waits)  # never shorter after another failure
    assert all(w <= hours * 3600 for w in waits)  # never past the interval
    assert waits[-1] == hours * 3600  # ...which it reaches and stays at


def test_backup_now_reports_whether_it_took_one(tmp_path, monkeypatch):
    """The loop used to be unable to tell: backup_now returned None either
    way, so a backup that failed inside _do_backup counted as a success and
    the retry came straight back."""
    s = _sched(tmp_path)
    sg = s._cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")
    _collect(s._bus)

    assert isinstance(asyncio.run(s.backup_now("scheduled")), backups.Backup)

    def dies(*a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(backups, "create", dies)
    assert asyncio.run(s.backup_now("scheduled")) is None


def test_the_loop_counts_failures_and_resets_on_success(tmp_path, monkeypatch):
    """Drive _backup_loop through fail, fail, succeed and watch the waits."""
    s = _sched(tmp_path)
    s._cfg.schedule.backup_hours = 6
    outcomes = iter([None, None, object(), None])
    waits: list[float] = []

    async def fake_backup(label):
        return next(outcomes)

    async def fake_sleep(secs):
        waits.append(secs)
        if len(waits) > 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(s, "backup_now", fake_backup)
    monkeypatch.setattr(s, "_seconds_until_backup_due", lambda hours: 300.0)
    monkeypatch.setattr(sched_mod.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s._backup_loop())

    # First wait: nothing failed yet. Then two failures push it out, and the
    # success drops it straight back to the schedule's own answer.
    assert waits[0] == 300.0
    assert waits[1] == _BACKUP_OVERDUE_GRACE * 2
    assert waits[2] == _BACKUP_OVERDUE_GRACE * 4
    assert waits[3] == 300.0


# ---------------- the pre-backup settle, and what the manifest knew ----------


def test_a_backup_waits_for_the_world_to_stop_changing(tmp_path, monkeypatch):
    s = _sched(tmp_path)
    sg = s._cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")
    _collect(s._bus)
    waited: list[Path] = []

    def fake_wait(root, **kw):
        waited.append(Path(root))
        return True

    monkeypatch.setattr(backups, "wait_for_quiet", fake_wait)
    asyncio.run(s.backup_now("scheduled"))
    assert waited == [sg]


def test_a_damaged_save_in_the_live_world_is_announced(tmp_path, monkeypatch):
    """The manifest recorded it; nobody said it. A backup of an already-broken
    save looked exactly like a good one."""
    s = _sched(tmp_path)
    sg = s._cfg.savegames_dir
    sg.mkdir(parents=True)
    # A save whose header promises more bytes than follow it.
    header = (500).to_bytes(4, "little") + (500).to_bytes(4, "little") + b"PlZ\x31"
    (sg / "Level.sav").write_bytes(header + b"x" * 100)
    events = _collect(s._bus)

    b = asyncio.run(s.backup_now("scheduled"))
    assert b is not None and b.problems
    warned = [e for e in events if e.kind == "error" and "damaged" in e.message]
    assert warned and "Level.sav" in warned[0].message
    assert warned[0].data["problems"] == list(b.problems)


# ---------------- a sleeping server ----------------


def test_a_sleeping_server_is_backed_up_without_asking_it_to_save(tmp_path):
    """Its world was saved and then stopped cleanly, so the API is silent on
    purpose — asking it would only produce the 'couldn't save' warning."""
    s = _sched(tmp_path, is_paused=lambda: True)
    sg = s._cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")
    events = _collect(s._bus)
    asked = []

    class _NoApi:
        async def save_best_effort(self, settle=0.0):
            asked.append(1)
            return False

    s._control = _NoApi()  # type: ignore[assignment]
    b = asyncio.run(s._do_backup("scheduled"))

    assert b is not None and asked == []
    assert backups.read_manifest(Path(s._cfg.backup_root), b.name)["flushed"] is True
    assert not any("Couldn't save" in e.message for e in events)


def test_the_daily_restart_skips_a_sleeping_server(monkeypatch):
    cfg = Config()
    cfg.schedule.enabled = True
    cfg.schedule.daily_restart = True
    bus = EventBus()
    events = _collect(bus)
    s = Scheduler(cfg, FakeApi(), bus, is_paused=lambda: True)
    monkeypatch.setattr(sched_mod, "next_restart_target", lambda *a: datetime.now())
    restarted: list = []

    async def fake_restart(reason, **kw):
        restarted.append(reason)
        return True

    monkeypatch.setattr(s, "restart_with_countdown", fake_restart)
    n = {"calls": 0}

    async def fake_sleep(_secs):
        # The loop's waits collapse to nothing; the fourth one ends the test.
        n["calls"] += 1
        if n["calls"] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(sched_mod.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s._daily_restart_loop())

    assert restarted == []
    assert any("asleep" in e.message for e in events)


# ---------------- the update check ----------------


def _patch_builds(monkeypatch, installed: str, latest_seq: list[str | None], seen=None):
    monkeypatch.setattr(sched_mod.steamcmd, "installed_buildid", lambda root, app: installed)
    remaining = list(latest_seq)

    async def _latest(sc, app, **kw):
        if seen is not None:
            seen.append(kw)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr(sched_mod.steamcmd, "latest_buildid", _latest)


def test_a_new_build_is_announced_once_not_on_every_check(tmp_path, monkeypatch):
    """The check runs hourly now. Hourly repeats of the same news are a nag,
    and a nag gets muted — which is the one thing an update notice must not
    become."""
    s = _sched(tmp_path)
    events = _collect(s._bus)
    _patch_builds(monkeypatch, "100", ["200"])

    for _ in range(4):
        assert asyncio.run(s.check_update_available()) is True
    assert len([e for e in events if e.kind == "update_available"]) == 1
    assert s.update_status["state"] == "behind"


def test_a_newer_build_still_is_news_and_so_is_falling_behind_again(tmp_path, monkeypatch):
    s = _sched(tmp_path)
    events = _collect(s._bus)
    _patch_builds(monkeypatch, "100", ["200", "300", "100", "400"])

    asyncio.run(s.check_update_available())  # 200: announced
    asyncio.run(s.check_update_available())  # 300: a different build, announced
    assert asyncio.run(s.check_update_available()) is False  # 100: current, quiet
    asyncio.run(s.check_update_available())  # 400: behind again, announced
    notices = [e for e in events if e.kind == "update_available"]
    assert [e.data["latest"] for e in notices] == ["200", "300", "400"]


def test_the_check_compares_against_the_held_branch(tmp_path, monkeypatch):
    s = _sched(tmp_path)
    s._cfg.steam_branch = "beta-old"
    seen: list[dict] = []
    _patch_builds(monkeypatch, "100", ["100"], seen)
    asyncio.run(s.check_update_available())
    assert seen and seen[0].get("branch") == "beta-old"


def test_the_check_stands_aside_while_steamcmd_is_busy(tmp_path, monkeypatch):
    """Two SteamCMDs in one directory trip over each other. While an update
    holds the lock the check answers from the standing status."""
    s = _sched(tmp_path)
    monkeypatch.setattr(sched_mod.steamcmd, "installed_buildid", lambda root, app: "100")

    async def must_not_run(sc, app, **kw):
        raise AssertionError("a second SteamCMD was started beside the first")

    monkeypatch.setattr(sched_mod.steamcmd, "latest_buildid", must_not_run)

    async def go(state: str) -> bool:
        s.update_status = {"state": state}
        async with s._steam_lock:
            return await s.check_update_available()

    assert asyncio.run(go("behind")) is True
    assert asyncio.run(go("current")) is False


def test_an_update_holds_the_steamcmd_lock_while_it_runs(tmp_path, monkeypatch):
    s = _sched(tmp_path)
    s._cfg.schedule.restart_countdown_seconds = 0
    held: list[bool] = []

    async def stop(name):
        return True

    async def start(name):
        return True

    monkeypatch.setattr(sched_mod.procs, "stop_service", stop)
    monkeypatch.setattr(sched_mod.procs, "start_service", start)
    monkeypatch.setattr(sched_mod.procs, "processes_under", lambda root: [])
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p, **kw: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)
    monkeypatch.setattr(sched_mod.steamcmd, "installed_buildid", lambda root, app: "200")

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line, **kw):
        held.append(s._steam_lock.locked())
        return 0

    async def fake_latest(sc, app, **kw):
        return "200"

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "latest_buildid", fake_latest)
    _collect(s._bus)

    asyncio.run(s.update_server(seconds=0))
    assert held == [True]
    assert not s._steam_lock.locked()  # released afterwards


def test_update_check_seconds_is_clamped_and_survives_garbage():
    assert update_check_seconds(60) == 3600.0
    assert update_check_seconds(1) == 600.0  # the floor
    assert update_check_seconds(0) == 600.0
    assert update_check_seconds("six") == 3600.0  # a hand-edited config
    assert update_check_seconds(None) == 3600.0


# ---------------- update-on-detect ----------------


def _on_detect_sched(tmp_path, monkeypatch, *, available: bool, **flags):
    s = _sched(tmp_path)
    s._cfg.schedule.enabled = True
    s._cfg.schedule.auto_update_on_detect = True
    for k, v in flags.items():
        setattr(s._cfg.schedule, k, v)
    ran: list = []

    async def fake_check():
        return available

    async def fake_update(**kw):
        ran.append(1)

    monkeypatch.setattr(s, "check_update_available", fake_check)
    monkeypatch.setattr(s, "update_server", fake_update)
    return s, ran


def test_update_when_available_installs_the_build_it_found(tmp_path, monkeypatch):
    s, ran = _on_detect_sched(tmp_path, monkeypatch, available=True)
    assert asyncio.run(s.update_when_available()) is True
    assert ran == [1]


def test_update_when_available_does_nothing_when_current(tmp_path, monkeypatch):
    s, ran = _on_detect_sched(tmp_path, monkeypatch, available=False)
    assert asyncio.run(s.update_when_available()) is False
    assert ran == []


def test_update_when_available_is_opt_in(tmp_path, monkeypatch):
    s, ran = _on_detect_sched(tmp_path, monkeypatch, available=True,
                              auto_update_on_detect=False)
    assert asyncio.run(s.update_when_available()) is False
    assert ran == []
    s, ran = _on_detect_sched(tmp_path, monkeypatch, available=True, enabled=False)
    assert asyncio.run(s.update_when_available()) is False
    assert ran == []


def test_update_when_available_leaves_a_deliberately_stopped_server_alone(
    tmp_path, monkeypatch
):
    s, ran = _on_detect_sched(tmp_path, monkeypatch, available=True)
    s._intent_running = lambda: False
    assert asyncio.run(s.update_when_available()) is False
    assert ran == []


def test_update_when_available_never_queues_behind_another_operation(tmp_path, monkeypatch):
    """A restore or a countdown holds the lock; the next check comes round
    soon enough, and the announcement already went out."""
    s, ran = _on_detect_sched(tmp_path, monkeypatch, available=True)

    async def go():
        async with s._control.operation("restore"):
            return await s.update_when_available()

    assert asyncio.run(go()) is False
    assert ran == []
