"""The backup schedule's two silent failures, and the worker-restart guard.

1. The schedule was restart-amnesiac: a flat `sleep(interval)` at the top of the
   loop meant every daemon restart put the clock back to zero, so a box that
   reboots more often than the interval never reached a backup — and nothing
   said so, because no backup had *failed*.
2. `_do_backup` threw away the answer from the pre-backup save, so a wedged REST
   API produced a stale world filed as a clean backup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from palctl import backups
from palctl.config import Config
from palctl.events import Event, EventBus
from palctl.scheduler import _BACKUP_OVERDUE_GRACE, Scheduler


def make(tmp_path: Path, **schedule) -> Scheduler:
    cfg = Config()
    cfg.backup_root = str(tmp_path / "backups")
    # savegames_dir is derived from server_root, not settable.
    cfg.server_root = str(tmp_path / "server")
    for k, v in schedule.items():
        setattr(cfg.schedule, k, v)
    return Scheduler(cfg, api=None, bus=EventBus())  # type: ignore[arg-type]


def collect(s: Scheduler) -> list[Event]:
    """The bus awaits its handlers, so a plain lambda logs a TypeError even
    though it does append. Give it a coroutine."""
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    s._bus.on_any(handler)
    return seen


def stamped(root: Path, when: datetime, label: str = "scheduled") -> None:
    (root / f"{when.strftime('%Y-%m-%d_%H-%M-%S')}-{label}").mkdir(parents=True)


# ---------------- when the next backup is due ----------------


def test_a_fresh_install_waits_a_full_interval(tmp_path):
    """Nothing to go on — don't back up a world the server may not have opened."""
    s = make(tmp_path)
    assert s._seconds_until_backup_due(6) == 6 * 3600


def test_the_wait_counts_from_the_last_backup_not_from_startup(tmp_path):
    """The fix. Four hours into a six-hour interval leaves two hours, however
    many times the daemon has restarted in between."""
    s = make(tmp_path)
    stamped(Path(s._cfg.backup_root), datetime.now() - timedelta(hours=4))

    due = s._seconds_until_backup_due(6)
    assert 2 * 3600 - 120 < due <= 2 * 3600


def test_an_overdue_backup_runs_after_a_short_grace_not_instantly(tmp_path):
    """A restart-loop used to reset the clock forever; now it catches up. But
    not the instant the daemon comes up — the server is still starting, and a
    copy taken across its first save is the torn backup this all avoids."""
    s = make(tmp_path)
    stamped(Path(s._cfg.backup_root), datetime.now() - timedelta(days=3))

    assert s._seconds_until_backup_due(24) == _BACKUP_OVERDUE_GRACE


def test_a_backup_stamped_in_the_future_cannot_park_the_loop(tmp_path):
    """A clock jump (or a hand-copied directory) must not push the next backup
    past one interval."""
    s = make(tmp_path)
    stamped(Path(s._cfg.backup_root), datetime.now() + timedelta(days=30))

    assert s._seconds_until_backup_due(6) == 6 * 3600


def test_an_unreadable_backup_root_falls_back_to_the_interval(tmp_path):
    s = make(tmp_path)
    s._cfg.backup_root = "\x00not-a-path"
    assert s._seconds_until_backup_due(3) == 3 * 3600


# ---------------- the dead-man's switch ----------------


def test_backups_switched_off_are_announced_once(tmp_path):
    s = make(tmp_path)
    stamped(Path(s._cfg.backup_root), datetime.now() - timedelta(days=9))
    seen = collect(s)

    asyncio.run(s._warn_if_backups_stale(24))
    asyncio.run(s._warn_if_backups_stale(24))  # second call must stay quiet

    assert len(seen) == 1
    assert "not running" in seen[0].message
    assert "9.0 days old" in seen[0].message


def test_no_warning_while_backups_are_merely_recent(tmp_path):
    s = make(tmp_path)
    stamped(Path(s._cfg.backup_root), datetime.now() - timedelta(hours=2))
    seen = collect(s)

    asyncio.run(s._warn_if_backups_stale(24))
    assert seen == []


def test_never_having_backed_up_is_itself_the_warning(tmp_path):
    s = make(tmp_path)
    Path(s._cfg.backup_root).mkdir(parents=True)
    seen = collect(s)

    asyncio.run(s._warn_if_backups_stale(24))
    assert len(seen) == 1
    assert "no backup has ever been taken" in seen[0].message


# ---------------- the pre-backup save ----------------


class FakeControl:
    """Enough ServerController to drive _do_backup."""

    def __init__(self, *, flushed: bool) -> None:
        self._flushed = flushed

    async def save_best_effort(self, settle: float = 0.0) -> bool:
        return self._flushed


def run_backup(s: Scheduler, monkeypatch, *, flushed: bool) -> dict:
    recorded: dict = {}

    def fake_create(savegames, root, label="manual", **kw):
        recorded.update(kw)
        d = Path(root) / "2026-01-01_00-00-00-test"
        d.mkdir(parents=True, exist_ok=True)
        return backups.Backup(d.name, d, 1.0, datetime.now(), True)

    monkeypatch.setattr(backups, "create", fake_create)
    monkeypatch.setattr(s, "_prune", lambda *a, **k: _async([]))
    monkeypatch.setattr(s, "_mirror", lambda *a, **k: _async(False))
    s._control = FakeControl(flushed=flushed)  # type: ignore[assignment]
    Path(s._cfg.savegames_dir).mkdir(parents=True, exist_ok=True)

    asyncio.run(s._do_backup("scheduled"))
    return recorded


def _async(value):
    async def inner():
        return value

    return inner()


def test_a_failed_save_is_announced_and_recorded(tmp_path, monkeypatch):
    """The headline fix: save_best_effort's answer used to be discarded, so a
    wedged API produced a stale world filed as a clean backup."""
    s = make(tmp_path)
    seen = collect(s)

    recorded = run_backup(s, monkeypatch, flushed=False)

    assert recorded["flushed"] is False
    assert any("Couldn't save before this backup" in e.message for e in seen)


def test_a_good_save_says_nothing_and_records_true(tmp_path, monkeypatch):
    s = make(tmp_path)
    seen = collect(s)

    recorded = run_backup(s, monkeypatch, flushed=True)

    assert recorded["flushed"] is True
    assert not any("Couldn't save" in e.message for e in seen)


def test_the_backup_still_happens_when_the_save_failed(tmp_path, monkeypatch):
    """An older world beats no world — the guard announces, it does not refuse."""
    s = make(tmp_path)
    seen = collect(s)

    run_backup(s, monkeypatch, flushed=False)

    assert any(e.kind == "backup" for e in seen)


@pytest.mark.parametrize("hours", [1, 6, 24])
def test_the_interval_is_always_the_ceiling(tmp_path, hours):
    s = make(tmp_path)
    stamped(Path(s._cfg.backup_root), datetime.now())
    assert s._seconds_until_backup_due(hours) <= hours * 3600
