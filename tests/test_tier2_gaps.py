"""The rest of the competitive-review gaps: branch pinning, the update
countdown, calendar retention, and the backup-volume warning.

Each of these is something every comparable manager does and palctl did not.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from palctl import backups, preflight, steamcmd
from palctl.config import Config
from palctl.events import Event, EventBus
from palctl.scheduler import _COUNTDOWN_OPS, Scheduler

# ---------------- Steam branch selection ----------------


def test_update_command_stays_on_public_by_default():
    cmd = steamcmd.update_command("steamcmd.exe", "C:/pal", "123", validate=False)
    assert "-beta" not in cmd


def test_update_command_can_hold_a_branch():
    """What keeps a server off a build Pocketpair shipped today."""
    cmd = steamcmd.update_command(
        "steamcmd.exe", "C:/pal", "123", validate=False, branch="v0.3.2"
    )
    assert cmd[cmd.index("-beta") + 1] == "v0.3.2"
    # -beta is an argument to app_update, so it must follow the app id...
    assert cmd.index("-beta") > cmd.index("123")
    # ...and precede +quit.
    assert cmd.index("-beta") < cmd.index("+quit")


def test_a_branch_password_only_rides_along_with_a_branch():
    cmd = steamcmd.update_command(
        "steamcmd.exe", "C:/pal", "123", validate=False, beta_password="hunter2"
    )
    assert "-betapassword" not in cmd

    cmd = steamcmd.update_command(
        "steamcmd.exe", "C:/pal", "123", validate=False,
        branch="beta", beta_password="hunter2",
    )
    assert cmd[cmd.index("-betapassword") + 1] == "hunter2"


def test_validate_still_lands_after_the_branch_arguments():
    """`validate` is an app_update argument too, and order matters to SteamCMD."""
    cmd = steamcmd.update_command(
        "steamcmd.exe", "C:/pal", "123", validate=True, branch="beta"
    )
    assert cmd.index("validate") > cmd.index("-beta")


# ---------------- the update countdown ----------------


def test_update_is_a_countdown_operation():
    """It takes the server down for longer than a restart does, and used to do
    it with no warning to anyone playing."""
    assert "update" in _COUNTDOWN_OPS


def make(tmp_path: Path) -> Scheduler:
    cfg = Config()
    cfg.backup_root = str(tmp_path / "backups")
    cfg.server_root = str(tmp_path / "server")
    exe = tmp_path / "steamcmd.exe"
    exe.write_text("")
    cfg.steamcmd_path = str(exe)
    return Scheduler(cfg, api=None, bus=EventBus())  # type: ignore[arg-type]


def collect(s: Scheduler) -> list[Event]:
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    s._bus.on_any(handler)
    return seen


def test_a_cancelled_countdown_leaves_the_server_untouched(tmp_path, monkeypatch):
    s = make(tmp_path)
    seen = collect(s)
    ran = False

    async def never(*a, **k):
        nonlocal ran
        ran = True

    async def cancelled(*a, **k):
        return False

    monkeypatch.setattr(s, "_update_locked", never)
    monkeypatch.setattr(s, "_count_down", cancelled)

    asyncio.run(s.update_server())

    assert not ran
    assert any("cancelled" in e.message.lower() for e in seen)


def test_the_update_runs_once_the_countdown_finishes(tmp_path, monkeypatch):
    s = make(tmp_path)
    ran = False

    async def update(*a, **k):
        nonlocal ran
        ran = True

    async def finished(*a, **k):
        return True

    monkeypatch.setattr(s, "_update_locked", update)
    monkeypatch.setattr(s, "_count_down", finished)

    asyncio.run(s.update_server())

    assert ran


def test_the_countdown_length_can_be_overridden_for_one_update(tmp_path, monkeypatch):
    """`--now` on a manual update has to mean now."""
    s = make(tmp_path)
    requested: list[int] = []

    async def record(kind, reason, seconds, **k):
        requested.append(seconds)
        return True

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(s, "_count_down", record)
    monkeypatch.setattr(s, "_update_locked", noop)

    asyncio.run(s.update_server(seconds=0))

    assert requested == [0]


# ---------------- calendar retention ----------------


def bset(*whens: datetime) -> list[backups.Backup]:
    """Backups newest-first, as listing() returns them."""
    ordered = sorted(whens, reverse=True)
    return [
        backups.Backup(
            w.strftime("%Y-%m-%d_%H-%M-%S") + "-scheduled", Path("/x"), 0.0, w
        )
        for w in ordered
    ]


def test_a_burst_of_backups_no_longer_evicts_the_whole_history():
    """The flat-count failure: twelve backups this afternoon is not a history,
    and the day you need one is usually the day you learn the corruption
    started last week."""
    now = datetime(2026, 6, 1, 12, 0, 0)
    today = [now - timedelta(minutes=5 * i) for i in range(12)]
    older = [now - timedelta(days=8), now - timedelta(days=40)]

    doomed = backups._doomed_scheduled(bset(*today, *older), 3, backups.KeepPolicy())
    doomed_names = {b.name for b in doomed}

    for old in older:
        assert old.strftime("%Y-%m-%d_%H-%M-%S") + "-scheduled" not in doomed_names


def test_the_flat_count_is_still_honoured_on_top():
    now = datetime(2026, 6, 1, 12, 0, 0)
    same_day = [now - timedelta(minutes=5 * i) for i in range(10)]

    kept = [
        b for b in bset(*same_day)
        if b not in backups._doomed_scheduled(bset(*same_day), 3, backups.KeepPolicy())
    ]
    # 3 newest by count, plus the one daily bucket they all share (already in
    # the 3) — so exactly the flat count survives a single-day burst.
    assert len(kept) == 3


def test_retention_can_be_put_back_to_flat_count_only():
    now = datetime(2026, 6, 1, 12, 0, 0)
    spread = [now - timedelta(days=i) for i in range(10)]

    doomed = backups._doomed_scheduled(bset(*spread), 2, None)
    assert len(doomed) == 8


def test_calendar_rules_can_only_ever_keep_more_than_the_flat_count():
    """The safety property that makes this layerable: a backup survives if ANY
    rule wants it, so switching the policy on can never delete something the
    old behaviour kept."""
    now = datetime(2026, 6, 1, 12, 0, 0)
    spread = bset(*[now - timedelta(days=i) for i in range(30)])

    flat = {b.name for b in backups._doomed_scheduled(spread, 5, None)}
    gfs = {b.name for b in backups._doomed_scheduled(spread, 5, backups.KeepPolicy())}

    assert gfs <= flat


def test_an_unparseable_backup_name_is_never_deleted_by_retention():
    odd = backups.Backup("2026-13-45_99-99-99-weird", Path("/x"), 0.0, datetime.now())
    doomed = backups._doomed_scheduled([odd], 0, backups.KeepPolicy())
    assert doomed == []


def test_prune_accepts_a_policy_end_to_end(tmp_path):
    root = tmp_path / "backups"
    now = datetime(2026, 6, 1, 12, 0, 0)
    for when in [now - timedelta(minutes=5 * i) for i in range(6)] + [
        now - timedelta(days=20)
    ]:
        (root / (when.strftime("%Y-%m-%d_%H-%M-%S") + "-scheduled")).mkdir(parents=True)

    backups.prune(root, 2, backups.KeepPolicy())

    survivors = {b.name for b in backups.listing(root, with_size=False)}
    assert (now - timedelta(days=20)).strftime("%Y-%m-%d_%H-%M-%S") + "-scheduled" in survivors


# ---------------- the backup-volume warning ----------------


def test_backups_beside_the_server_are_flagged_but_not_failed(tmp_path):
    """One disk is a reasonable place to start and a bad place to stop — a
    warning, never a blocker."""
    (tmp_path / "server").mkdir()
    (tmp_path / "backups").mkdir()

    check = preflight.check_backup_volume(tmp_path / "server", tmp_path / "backups")

    assert check.ok is None  # not a failure
    assert "same disk" in check.detail
    assert check.fix


def test_the_check_is_skipped_when_no_backup_root_is_known(tmp_path):
    names = [c.name for c in preflight.run_all(tmp_path, 8212, need_install=False)]
    assert "Backup location" not in names


def test_the_check_runs_once_a_backup_root_is_given(tmp_path):
    names = [
        c.name
        for c in preflight.run_all(
            tmp_path, 8212, need_install=False, backup_root=str(tmp_path / "b")
        )
    ]
    assert "Backup location" in names


@pytest.mark.parametrize("missing", ["server", "backups"])
def test_an_unresolvable_path_is_unknown_not_alarming(tmp_path, missing):
    paths = {"server": tmp_path / "server", "backups": tmp_path / "backups"}
    for name, p in paths.items():
        if name != missing:
            p.mkdir()

    check = preflight.check_backup_volume(paths["server"], paths["backups"])
    assert check.ok is None
