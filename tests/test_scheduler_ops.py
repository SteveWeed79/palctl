"""The update and restore flows take the server down and bring it back around a
destructive step (SteamCMD validate; overwriting SaveGames). The ordering and the
ini auto-restore are the parts that ruin someone's day if wrong, so they're
pinned here with the real orchestration and faked side effects."""

import asyncio
from pathlib import Path

from palctl import scheduler as sched_mod
from palctl.config import Config
from palctl.events import EventBus


class FakeApi:
    # `online` drives the countdown-collapse rule: an empty server has nobody
    # to warn, so the wait is cut short. None = the REST API isn't answering,
    # which reads the same way (an announcement reaches nobody either).
    def __init__(self, online: int | None = 1):
        self.online = online
        self.announced: list[str] = []

    async def save(self):
        pass

    async def wait_until_alive(self, timeout=240):
        return True

    async def announce(self, message):
        self.announced.append(message)

    async def players(self):
        if self.online is None:
            raise RuntimeError("REST API not answering")
        return [object()] * self.online


def _no_countdown(cfg: Config) -> Config:
    """Most of these tests are about what happens *after* the warning, so turn
    it off rather than sleeping through it."""
    cfg.schedule.restart_countdown_seconds = 0
    cfg.schedule.restore_countdown_seconds = 0
    return cfg


def _collect(bus: EventBus) -> list:
    events: list = []

    async def handler(e):
        events.append(e)

    bus.on_any(handler)
    return events


def _run(coro):
    return asyncio.run(coro)


def _patch_service(monkeypatch, calls):
    async def stop(name):
        calls.append(("stop", name))
        return True

    async def start(name):
        calls.append(("start", name))
        return True

    monkeypatch.setattr(sched_mod.procs, "stop_service", stop)
    monkeypatch.setattr(sched_mod.procs, "start_service", start)


# ---------------- update ----------------


def test_update_server_stops_updates_then_starts(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")

    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        calls.append(("update", str(install_dir), app_id, validate))
        if on_line:
            on_line("Success! App '2394010' fully installed.")
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert [c[0] for c in calls] == ["stop", "update", "start"]
    # The update ran against the configured install dir and app id — and
    # WITHOUT validate. `validate` is a full re-verification of every file
    # against Steam's manifest, which restores anything that differs; it is
    # what resets PalWorldSettings.ini, and it has no place in a routine
    # update. Plain app_update still installs the newest build.
    assert calls[1][1] == cfg.server_root and calls[1][2] == cfg.app_id
    assert calls[1][3] is False, "routine updates must not validate"
    assert any(e.kind == "update" and "back up" in e.message for e in events)


def test_update_server_restores_blanked_ini(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")

    _patch_service(monkeypatch, [])

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        return 0

    fake_bak = tmp_path / "PalWorldSettings.ini.bak"
    copied: list = []
    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: fake_bak)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: True)  # validate wiped it
    monkeypatch.setattr(sched_mod.shutil, "copy2", lambda a, b: copied.append((a, b)))

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert copied and copied[0][0] == fake_bak  # ini restored from the pre-update backup
    assert any("restored it" in e.message for e in events)


def test_update_server_takes_pre_update_backup(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    # A real SaveGames to back up — updates are when saves get eaten.
    sg = cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")

    _patch_service(monkeypatch, [])

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    made = [d.name for d in Path(cfg.backup_root).iterdir()]
    assert len(made) == 1 and made[0].endswith("-pre-update")
    assert not any("Pre-update backup failed" in e.message for e in events)


def test_update_server_mirrors_backup_when_configured(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    cfg.backup_mirror = str(tmp_path / "mirror")
    cfg.backup_mirror_enabled = True
    sg = cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")

    _patch_service(monkeypatch, [])

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)

    bus = EventBus()
    _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    primary = [d.name for d in Path(cfg.backup_root).iterdir()]
    mirrored = [d.name for d in Path(cfg.backup_mirror).iterdir()]
    assert primary == mirrored and len(mirrored) == 1


def test_mirror_dispatches_to_rclone_for_a_remote_target(tmp_path, monkeypatch):
    # A `remote:path` mirror goes through rclone (cloud), not the local copy.
    from palctl import backups

    cfg = Config()
    cfg.backup_mirror = "gdrive:PalworldBackups"
    cfg.backup_mirror_enabled = True
    cfg.schedule.backup_retain = 7

    calls: list = []
    monkeypatch.setattr(sched_mod.rclone, "mirror",
                        lambda path, remote: calls.append(("mirror", str(path), remote)))
    monkeypatch.setattr(sched_mod.rclone, "prune",
                        lambda remote, retain: calls.append(("prune", remote, retain)))
    # If dispatch is wrong and it falls through to the local copy, this fails loud.
    def _boom(*a, **k):
        raise AssertionError("local mirror used for a remote target")
    monkeypatch.setattr(backups, "mirror", _boom)

    bus = EventBus()
    errors = _collect(bus)
    b = backups.Backup("2026-07-15_10-00-00-scheduled", tmp_path / "b", 1.0,
                       __import__("datetime").datetime.now())
    ok = _run(sched_mod.Scheduler(cfg, FakeApi(), bus)._mirror(b))

    assert ok is True
    assert calls == [
        ("mirror", str(tmp_path / "b"), "gdrive:PalworldBackups"),
        ("prune", "gdrive:PalworldBackups", 7),
    ]
    assert not errors  # a clean run emits no error event


def test_mirror_retain_overrides_local_retention(tmp_path, monkeypatch):
    # The mirror can keep a different number of copies than the local disk.
    from palctl import backups

    cfg = Config()
    cfg.backup_mirror = "gdrive:PalworldBackups"
    cfg.backup_mirror_enabled = True
    cfg.schedule.backup_retain = 24
    cfg.schedule.mirror_retain = 5  # keep fewer off-site

    seen: list = []
    monkeypatch.setattr(sched_mod.rclone, "mirror", lambda path, remote: None)
    monkeypatch.setattr(sched_mod.rclone, "prune",
                        lambda remote, retain: seen.append(retain))

    bus = EventBus()
    _collect(bus)
    b = backups.Backup("2026-07-15_10-00-00-scheduled", tmp_path / "b", 1.0,
                       __import__("datetime").datetime.now())
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus)._mirror(b))

    assert seen == [5]  # mirror_retain, not backup_retain


def test_mirror_retain_zero_falls_back_to_local_retention(tmp_path, monkeypatch):
    from palctl import backups

    cfg = Config()
    cfg.backup_mirror = str(tmp_path / "mirror")
    cfg.backup_mirror_enabled = True
    cfg.schedule.backup_retain = 12
    cfg.schedule.mirror_retain = 0  # default: match local

    seen: list = []
    monkeypatch.setattr(sched_mod.backups, "mirror", lambda path, root: None)
    monkeypatch.setattr(sched_mod.backups, "prune",
                        lambda root, retain: seen.append(retain) or [])

    bus = EventBus()
    _collect(bus)
    b = backups.Backup("2026-07-15_10-00-00-scheduled", tmp_path / "b", 1.0,
                       __import__("datetime").datetime.now())
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus)._mirror(b))

    assert seen == [12]  # fell back to backup_retain


def test_mirror_rclone_failure_is_non_fatal(tmp_path, monkeypatch):
    # A cloud mirror failure must not fail the backup — it reports and returns False.
    from palctl import backups

    cfg = Config()
    cfg.backup_mirror = "gdrive:PalworldBackups"
    cfg.backup_mirror_enabled = True

    def _fail(*a, **k):
        raise RuntimeError("rclone: quota exceeded")
    monkeypatch.setattr(sched_mod.rclone, "mirror", _fail)

    bus = EventBus()
    errors = _collect(bus)
    b = backups.Backup("2026-07-15_10-00-00-scheduled", tmp_path / "b", 1.0,
                       __import__("datetime").datetime.now())
    ok = _run(sched_mod.Scheduler(cfg, FakeApi(), bus)._mirror(b))

    assert ok is False
    assert any("quota exceeded" in e.message for e in errors)


def test_mirror_skipped_when_off_site_disabled(tmp_path, monkeypatch):
    # A configured mirror target that's switched off must not be copied to — the
    # path is kept for later, but backup_mirror_enabled=False means no off-site
    # copy and no rclone/local-mirror call at all.
    from palctl import backups

    cfg = Config()
    cfg.backup_mirror = "gdrive:PalworldBackups"
    cfg.backup_mirror_enabled = False

    def _boom(*a, **k):
        raise AssertionError("mirror ran while off-site backups were disabled")
    monkeypatch.setattr(sched_mod.rclone, "mirror", _boom)
    monkeypatch.setattr(sched_mod.backups, "mirror", _boom)

    bus = EventBus()
    errors = _collect(bus)
    b = backups.Backup("2026-07-15_10-00-00-scheduled", tmp_path / "b", 1.0,
                       __import__("datetime").datetime.now())
    ok = _run(sched_mod.Scheduler(cfg, FakeApi(), bus)._mirror(b))

    assert ok is False
    assert not errors  # disabled is a clean no-op, not an error


def test_update_server_aborts_when_server_wont_stop(tmp_path, monkeypatch):
    # SteamCMD rewriting the install under a still-running server corrupts it:
    # a stop that never confirms STOPPED must abort the update untouched.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")

    calls: list = []

    async def stop_fails(name):
        calls.append(("stop", name))
        return False

    async def start(name):
        calls.append(("start", name))
        return True

    monkeypatch.setattr(sched_mod.procs, "stop_service", stop_fails)
    monkeypatch.setattr(sched_mod.procs, "start_service", start)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        calls.append(("update",))
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert ("update",) not in calls  # SteamCMD never ran
    assert [c[0] for c in calls] == ["stop"]  # and we didn't blind-start either
    assert any(e.kind == "error" and "did not stop" in e.message for e in events)


def test_update_server_aborts_when_backup_of_existing_world_fails(tmp_path, monkeypatch):
    # Updates are exactly when saves get eaten; if there IS a world and the
    # pre-update backup fails, proceeding means a bad update can't be rolled
    # back. Default behaviour: abort with the server untouched.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    sg = cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")

    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        calls.append(("update",))
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)

    def backup_dies(*a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(sched_mod.backups, "create", backup_dies)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert calls == []  # never stopped, never updated, never blind-started
    assert any(e.kind == "error" and "Update aborted" in e.message for e in events)


def test_update_server_backup_failure_opt_out_continues(tmp_path, monkeypatch):
    # The escape hatch: update_requires_backup=False restores the old
    # warn-and-continue behaviour for people whose backups live somewhere flaky.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    cfg.schedule.update_requires_backup = False
    sg = cfg.savegames_dir
    sg.mkdir(parents=True)
    (sg / "Level.sav").write_bytes(b"world")

    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        calls.append(("update",))
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)

    def backup_dies(*a, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(sched_mod.backups, "create", backup_dies)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert [c[0] for c in calls] == ["stop", "update", "start"]
    assert any("continuing with the update anyway" in e.message for e in events)


def test_update_server_fresh_install_skips_backup_and_proceeds(tmp_path, monkeypatch):
    # No SaveGames yet means nothing to protect (same rule as the wizard) —
    # requiring a backup of a world that doesn't exist would wedge first-time
    # installs.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")

    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        calls.append(("update",))
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert [c[0] for c in calls] == ["stop", "update", "start"]
    assert any("skipping the pre-update backup" in e.message for e in events)
    assert not any(e.kind == "error" for e in events)


def test_update_server_reports_update_exceptions(tmp_path, monkeypatch):
    # A GUI/bot-triggered update that throws used to restart the server and
    # announce success with no trace of the failure.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")

    _patch_service(monkeypatch, [])

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        raise OSError("steamcmd exploded")

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert any(e.kind == "error" and "steamcmd exploded" in e.message for e in events)
    # The server is still brought back — on the old files, but running.
    assert any(e.kind == "update" and "back up" in e.message for e in events)


def test_update_server_restores_ini_even_when_steamcmd_dies(tmp_path, monkeypatch):
    # SteamCMD can blank the ini and then die; the settings must come back
    # before the server is restarted, not only on the success path.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")

    _patch_service(monkeypatch, [])

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        raise OSError("dropped connection")

    fake_bak = tmp_path / "PalWorldSettings.ini.bak"
    copied: list = []
    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: fake_bak)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: True)
    monkeypatch.setattr(sched_mod.shutil, "copy2", lambda a, b: copied.append((a, b)))

    bus = EventBus()
    _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert copied and copied[0][0] == fake_bak


def test_update_server_aborts_without_steamcmd(tmp_path, monkeypatch):
    cfg = Config()
    cfg.steamcmd_path = str(tmp_path / "missing-steamcmd.exe")

    calls: list = []
    _patch_service(monkeypatch, calls)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert not calls  # never touched the service
    assert any(e.kind == "error" for e in events)


# ---------------- restore ----------------


def test_restore_backup_stops_restores_then_starts(tmp_path, monkeypatch):
    cfg = _no_countdown(Config())
    cfg.backup_root = str(tmp_path / "backups")
    cfg.server_root = str(tmp_path / "server")
    name = "2026-01-01_00-00-00-manual"
    (Path(cfg.backup_root) / name).mkdir(parents=True)

    calls: list = []
    _patch_service(monkeypatch, calls)
    monkeypatch.setattr(
        sched_mod.backups, "restore",
        lambda root, n, savegames: calls.append(("restore", n)),
    )

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).restore_backup(name))

    assert [c[0] for c in calls] == ["stop", "restore", "start"]
    assert any(e.kind == "restore" and "back up" in e.message for e in events)


def test_restore_backup_aborts_when_server_wont_stop(tmp_path, monkeypatch):
    # Copying over a live save corrupts it: a stop that never confirms STOPPED
    # must leave the world untouched.
    cfg = _no_countdown(Config())
    cfg.backup_root = str(tmp_path / "backups")
    cfg.server_root = str(tmp_path / "server")
    name = "2026-01-01_00-00-00-manual"
    (Path(cfg.backup_root) / name).mkdir(parents=True)

    calls: list = []

    async def stop_fails(n):
        calls.append(("stop", n))
        return False

    async def start(n):
        calls.append(("start", n))
        return True

    monkeypatch.setattr(sched_mod.procs, "stop_service", stop_fails)
    monkeypatch.setattr(sched_mod.procs, "start_service", start)
    monkeypatch.setattr(
        sched_mod.backups, "restore",
        lambda root, n, savegames: calls.append(("restore", n)),
    )

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).restore_backup(name))

    assert [c[0] for c in calls] == ["stop"]  # no restore, no blind start
    assert any(e.kind == "error" and "did not stop" in e.message for e in events)


def test_restore_backup_rejects_traversal_without_stopping(tmp_path, monkeypatch):
    cfg = Config()
    cfg.backup_root = str(tmp_path)

    calls: list = []
    _patch_service(monkeypatch, calls)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).restore_backup("../secrets"))

    assert not calls  # a bad name must not take the server down
    assert any(e.kind == "error" for e in events)


def test_restore_backup_rejects_empty_name_without_stopping(tmp_path, monkeypatch):
    # An empty (or ".") name resolves to backup_root itself; it must be rejected
    # before the server is stopped and the world overwritten.
    cfg = Config()
    cfg.backup_root = str(tmp_path)

    calls: list = []
    _patch_service(monkeypatch, calls)

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).restore_backup(""))

    assert not calls
    assert any(e.kind == "error" for e in events)


# ---------------- intentional-stop awareness ----------------


def test_intentionally_stopped_reflects_intent_callback():
    cfg = Config()
    # No callback (standalone/tests) == always "running", so loops behave as before.
    assert sched_mod.Scheduler(cfg, FakeApi(), EventBus())._intentionally_stopped() is False
    stopped = sched_mod.Scheduler(cfg, FakeApi(), EventBus(), intent_running=lambda: False)
    assert stopped._intentionally_stopped() is True
    running = sched_mod.Scheduler(cfg, FakeApi(), EventBus(), intent_running=lambda: True)
    assert running._intentionally_stopped() is False


def test_daily_restart_loop_skips_when_intentionally_stopped(monkeypatch):
    # The core of the fix: a server the admin stopped must NOT be restarted by
    # the scheduled daily restart.
    from datetime import datetime

    cfg = Config()
    cfg.schedule.enabled = True
    cfg.schedule.daily_restart = True

    bus = EventBus()
    events = _collect(bus)
    sched = sched_mod.Scheduler(cfg, FakeApi(), bus, intent_running=lambda: False)

    monkeypatch.setattr(sched, "_next_restart", lambda: datetime.now())
    restarted: list = []

    async def fake_restart(reason):
        restarted.append(reason)

    monkeypatch.setattr(sched, "restart_with_countdown", fake_restart)

    # Drive the infinite loop for a couple of iterations, then break out via a
    # patched sleep that yields control but stops us after a few calls.
    real_sleep = asyncio.sleep
    n = {"calls": 0}

    async def fake_sleep(_secs):
        n["calls"] += 1
        if n["calls"] > 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(sched_mod.asyncio, "sleep", fake_sleep)

    try:
        _run(sched._daily_restart_loop())
    except asyncio.CancelledError:
        pass

    assert not restarted  # never restarted a deliberately-stopped server
    assert any("Skipped" in e.message for e in events)


# ---------------- update-available check ----------------


def _patch_buildids(monkeypatch, installed, latest):
    monkeypatch.setattr(sched_mod.steamcmd, "installed_buildid", lambda root, app: installed)

    async def _latest(sc, app):
        return latest

    monkeypatch.setattr(sched_mod.steamcmd, "latest_buildid", _latest)


def test_update_available_notifies_when_builds_differ(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path)
    _patch_buildids(monkeypatch, installed="100", latest="200")

    bus = EventBus()
    events = _collect(bus)
    assert _run(sched_mod.Scheduler(cfg, FakeApi(), bus).check_update_available()) is True
    assert any(e.kind == "update_available" for e in events)


def test_update_available_quiet_when_current(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path)
    _patch_buildids(monkeypatch, installed="100", latest="100")

    bus = EventBus()
    events = _collect(bus)
    assert _run(sched_mod.Scheduler(cfg, FakeApi(), bus).check_update_available()) is False
    assert not any(e.kind == "update_available" for e in events)


def test_the_build_comparison_is_kept_not_only_announced(tmp_path, monkeypatch):
    """A version mismatch is the failure players hit before the admin does —
    they're refused at the join screen while palctl correctly reports a healthy
    server. An event scrolls away; /state has to be able to say "behind"."""
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path)
    _patch_buildids(monkeypatch, installed="100", latest="200")

    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus())
    assert sched.update_status["state"] == "unknown"  # nothing checked yet
    _run(sched.check_update_available())
    assert sched.update_status["state"] == "behind"
    assert sched.update_status["installed"] == "100"
    assert sched.update_status["latest"] == "200"
    assert sched.update_status["checked_at"]


def test_a_current_server_records_current(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path)
    _patch_buildids(monkeypatch, installed="100", latest="100")

    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus())
    _run(sched.check_update_available())
    assert sched.update_status["state"] == "current"


def test_a_blind_check_records_unknown_with_a_reason(tmp_path, monkeypatch):
    """"Can't tell" must never render as "up to date" — that's how a server sits
    on an old build for a week with nothing looking wrong."""
    cfg = Config()
    cfg.steamcmd_path = ""  # never configured
    cfg.server_root = str(tmp_path)
    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus())
    _run(sched.check_update_available())
    assert sched.update_status["state"] == "unknown"
    assert sched.update_status["detail"]

    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg.steamcmd_path = str(steam)
    _patch_buildids(monkeypatch, installed=None, latest="200")
    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus())
    _run(sched.check_update_available())
    assert sched.update_status["state"] == "unknown"
    assert "appmanifest" in sched.update_status["detail"]


# ---------------- manual start / stop (bot & GUI parity) ----------------


def test_start_server_records_intent_and_starts(monkeypatch):
    cfg = Config()
    calls: list = []
    _patch_service(monkeypatch, calls)
    intent: list = []
    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus(), set_intent=intent.append)
    assert _run(sched.start_server()) == "ok"
    assert intent == [True]  # 'server should be up' persisted
    assert ("start", cfg.service_name) in calls


def test_stop_server_saves_records_intent_and_stops(monkeypatch):
    cfg = Config()
    calls: list = []
    _patch_service(monkeypatch, calls)
    intent: list = []
    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus(), set_intent=intent.append)
    assert _run(sched.stop_server()) == "ok"
    assert intent == [False]  # a Stop must not be undone by auto-recovery
    assert ("stop", cfg.service_name) in calls


def test_reserve_blocks_a_second_op_and_reports_the_current_one():
    # The bot's /restart and /update reserve up front so a second one reports
    # 'busy' instead of silently queueing a second countdown.
    sched = sched_mod.Scheduler(Config(), FakeApi(), EventBus())
    assert sched.reserve("restart") is True
    assert sched.current_op == "restart"
    assert sched.reserve("update") is False  # busy — something already holds it
    sched.clear_reservation("restart")
    assert sched.current_op is None
    assert sched.reserve("update") is True  # free again


def test_reserved_restart_runs_and_clears_the_reservation(monkeypatch):
    # The bot flow: reserve("restart") → spawn restart_with_countdown → the
    # operation lock clears the reservation, the restart runs, and the finally
    # clears again. Proves the reserve+run path can't deadlock or leak a
    # reservation. Countdown sleeps are faked so the test is instant.
    cfg = _no_countdown(Config())
    calls: list = []
    _patch_service(monkeypatch, calls)

    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus())

    assert sched.reserve("restart") is True

    async def _drive():
        # Mirror the bot: run the reserved op through _run_reserved's contract.
        try:
            return await sched.restart_with_countdown("test")
        finally:
            sched.clear_reservation("restart")

    assert _run(_drive()) is True
    assert ("stop", cfg.service_name) in calls
    assert ("start", cfg.service_name) in calls
    # Reservation released, server free for the next op.
    assert sched.current_op is None
    assert sched.reserve("update") is True


def test_stop_server_reports_failure_when_stop_does_not_confirm(monkeypatch):
    cfg = Config()

    async def stop(name):
        return False  # service never reached STOPPED

    async def start(name):
        return True

    monkeypatch.setattr(sched_mod.procs, "stop_service", stop)
    monkeypatch.setattr(sched_mod.procs, "start_service", start)
    intent: list = []
    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus(), set_intent=intent.append)
    assert _run(sched.stop_server()) == "failed"
    assert intent == [False]  # the admin still asked to stop


def test_start_server_when_busy_returns_busy_without_claiming_intent(monkeypatch):
    cfg = Config()
    calls: list = []
    _patch_service(monkeypatch, calls)
    intent: list = []
    sched = sched_mod.Scheduler(cfg, FakeApi(), EventBus(), set_intent=intent.append)

    async def go():
        async with sched._control.operation("restart"):  # something else holds the lock
            return await sched.start_server()

    assert _run(go()) == "busy"
    assert intent == []  # never touched intent
    assert not any(c[0] == "start" for c in calls)


# ---------------- cancelling and skipping a countdown ----------------
#
# The admin-facing half of every take-the-server-down operation. Both escape
# hatches, on both operations, plus the three answers the surfaces give back —
# "too late" is deliberately not the same reply as "nothing was running".


async def _armed(sched, timeout_ticks: int = 1000):
    """Yield to the loop until the countdown has registered itself."""
    for _ in range(timeout_ticks):
        if sched._countdown is not None:
            return True
        await asyncio.sleep(0)
    return False


def _countdown_sched(monkeypatch, calls, *, seconds=30, online=1, **kw):
    cfg = Config()
    cfg.schedule.restart_countdown_seconds = seconds
    cfg.schedule.restore_countdown_seconds = seconds
    _patch_service(monkeypatch, calls)
    bus = EventBus()
    return cfg, bus, sched_mod.Scheduler(cfg, FakeApi(online), bus, **kw)


def test_cancel_and_skip_report_idle_when_nothing_is_running():
    sched = sched_mod.Scheduler(Config(), FakeApi(), EventBus())
    assert sched.cancel_countdown() == "idle"
    assert sched.skip_countdown() == "idle"
    assert sched.countdown_state() is None


def test_only_operations_that_count_down_can_be_arrived_at_too_late():
    """"Too late" means the admin missed a window. A backup, an update or the
    boot-time start never had one, so answering their Cancel with "too late"
    sends them looking for a countdown that never existed — and, because the
    daemon starts the server at boot, made the answer depend on how recently
    the machine came up."""
    sched = sched_mod.Scheduler(Config(), FakeApi(), EventBus())

    async def verdict_while(op: str) -> tuple[str, str]:
        async with sched._control.operation(op):
            return sched.cancel_countdown(), sched.skip_countdown()

    for op in ("restart", "restore"):
        assert _run(verdict_while(op)) == ("too_late", "too_late"), op
    for op in ("backup", "update", "start", "stop", "auto-recover", "watchdog-restart"):
        assert _run(verdict_while(op)) == ("idle", "idle"), op


def test_restart_countdown_can_be_cancelled_before_restart(monkeypatch):
    calls: list = []
    intent: list = []
    _cfg, bus, sched = _countdown_sched(
        monkeypatch, calls, set_intent=intent.append
    )
    events = _collect(bus)

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        assert sched.cancel_countdown() == "cancelled"
        # A second cancel has nothing left to take: the verdict is already in.
        assert sched.cancel_countdown() == "too_late"
        return await task

    result = _run(go())
    assert result is False  # the scheduled loop needs this to skip today's slot
    assert not any(c[0] in ("stop", "start") for c in calls)  # never restarted
    assert any("cancelled" in e.message.lower() for e in events)
    assert sched._countdown is None  # cleaned up
    assert True in intent  # a restart records 'server should be up' at entry


def test_restart_countdown_can_be_skipped_and_still_restarts(monkeypatch):
    # The half that didn't exist: an admin who doesn't want to wait out a
    # countdown they started gets the restart *now*, not a cancelled one.
    calls: list = []
    _cfg, bus, sched = _countdown_sched(monkeypatch, calls)
    events = _collect(bus)

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        assert sched.skip_countdown() == "skipped"
        return await task

    assert _run(go()) is True  # it ran — a skip is not a cancel
    assert [c[0] for c in calls] == ["stop", "start"]
    assert any("skipped the countdown" in e.message.lower() for e in events)


def test_restore_countdown_can_be_cancelled_leaving_the_world_alone(tmp_path, monkeypatch):
    calls: list = []
    cfg, bus, sched = _countdown_sched(monkeypatch, calls)
    cfg.backup_root = str(tmp_path / "backups")
    cfg.server_root = str(tmp_path / "server")
    name = "2026-01-01_00-00-00-manual"
    (Path(cfg.backup_root) / name).mkdir(parents=True)
    monkeypatch.setattr(
        sched_mod.backups, "restore",
        lambda root, n, savegames: calls.append(("restore", n)),
    )
    events = _collect(bus)

    async def go():
        task = asyncio.create_task(sched.restore_backup(name))
        assert await _armed(sched)
        assert sched.cancel_countdown() == "cancelled"
        return await task

    assert _run(go()) is False
    # The whole point: a cancelled restore never stopped the server, never
    # touched SaveGames, and never restarted anything.
    assert calls == []
    assert any("cancelled" in e.message.lower() for e in events)


def test_restore_countdown_can_be_skipped(tmp_path, monkeypatch):
    calls: list = []
    cfg, _bus, sched = _countdown_sched(monkeypatch, calls)
    cfg.backup_root = str(tmp_path / "backups")
    cfg.server_root = str(tmp_path / "server")
    name = "2026-01-01_00-00-00-manual"
    (Path(cfg.backup_root) / name).mkdir(parents=True)
    monkeypatch.setattr(
        sched_mod.backups, "restore",
        lambda root, n, savegames: calls.append(("restore", n)),
    )

    async def go():
        task = asyncio.create_task(sched.restore_backup(name))
        assert await _armed(sched)
        assert sched.skip_countdown() == "skipped"
        return await task

    assert _run(go()) is True
    assert [c[0] for c in calls] == ["stop", "restore", "start"]


def test_countdown_state_is_published_while_it_runs(monkeypatch):
    calls: list = []
    _cfg, _bus, sched = _countdown_sched(monkeypatch, calls, seconds=45)

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        state = sched.countdown_state()
        sched.cancel_countdown()
        await task
        return state

    state = _run(go())
    assert state["kind"] == "restart"
    assert state["reason"] == "test"
    assert state["total_seconds"] == 45
    assert 0 < state["seconds_remaining"] <= 45
    assert state["cancellable"] is True


def test_countdown_collapses_when_nobody_is_online(monkeypatch):
    # The complaint this whole feature came from: a ten-minute warning
    # announced to an empty server is ten minutes of nothing.
    calls: list = []
    _cfg, bus, sched = _countdown_sched(monkeypatch, calls, seconds=600, online=0)
    events = _collect(bus)

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        total = sched.countdown_state()["total_seconds"]
        sched.skip_countdown()  # don't actually wait even the short one out
        await task
        return total

    assert _run(go()) == sched_mod.countdown.EMPTY_SERVER_SECONDS
    assert any("nobody is online" in e.message for e in events)


def test_countdown_collapses_when_the_api_is_not_answering(monkeypatch):
    # An unreachable REST API means the announcement reaches nobody either, so
    # waiting it out buys exactly as much as an empty server does.
    calls: list = []
    _cfg, _bus, sched = _countdown_sched(monkeypatch, calls, seconds=600, online=None)

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        total = sched.countdown_state()["total_seconds"]
        sched.skip_countdown()
        await task
        return total

    assert _run(go()) == sched_mod.countdown.EMPTY_SERVER_SECONDS


def test_countdown_is_kept_in_full_when_players_are_online(monkeypatch):
    calls: list = []
    _cfg, _bus, sched = _countdown_sched(monkeypatch, calls, seconds=600, online=3)

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        total = sched.countdown_state()["total_seconds"]
        sched.cancel_countdown()
        await task
        return total

    assert _run(go()) == 600  # players deserve the notice they were promised


def test_collapse_can_be_turned_off(monkeypatch):
    calls: list = []
    cfg, _bus, sched = _countdown_sched(monkeypatch, calls, seconds=600, online=0)
    cfg.schedule.skip_countdown_when_empty = False

    async def go():
        task = asyncio.create_task(sched.restart_with_countdown("test"))
        assert await _armed(sched)
        total = sched.countdown_state()["total_seconds"]
        sched.cancel_countdown()
        await task
        return total

    assert _run(go()) == 600


def test_explicit_seconds_override_the_configured_countdown(monkeypatch):
    calls: list = []
    _cfg, bus, sched = _countdown_sched(monkeypatch, calls, seconds=600, online=3)
    events = _collect(bus)
    # seconds=0 is the "go now, don't warn anybody" escape hatch.
    assert _run(sched.restart_with_countdown("test", seconds=0)) is True
    assert [c[0] for c in calls] == ["stop", "start"]
    assert any("no countdown" in e.message for e in events)


def test_restart_countdown_returns_true_when_it_runs(monkeypatch):
    # The counterpart to the cancel case: a completed restart reports True, so the
    # daily loop knows it happened (and won't be told to skip the day).
    calls: list = []
    _cfg, _bus, sched = _countdown_sched(monkeypatch, calls, seconds=0)
    assert _run(sched.restart_with_countdown("test")) is True
    assert [c[0] for c in calls] == ["stop", "start"]  # it actually restarted


def test_update_server_records_up_intent(tmp_path, monkeypatch):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        if on_line:
            on_line("done")
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)
    intent: list = []
    _run(
        sched_mod.Scheduler(cfg, FakeApi(), EventBus(), set_intent=intent.append).update_server()
    )
    assert True in intent  # parity with the daemon's HTTP /action/update-server


# ---------- the update actually landing (the version-mismatch bug) ----------


def _update_cfg(tmp_path):
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    return cfg


def _patch_steamcmd(monkeypatch, calls, *, installed, latest="200"):
    """Fake SteamCMD whose install leaves `installed` behind as the build id
    (a list, popped per read, so before/after can differ)."""

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line):
        calls.append(("update",))
        return 0

    async def fake_latest(sc, app):
        return latest

    reads = list(installed)
    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "latest_buildid", fake_latest)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)
    monkeypatch.setattr(sched_mod, "is_blank", lambda p: False)
    monkeypatch.setattr(
        sched_mod.steamcmd, "installed_buildid",
        lambda root, app: reads.pop(0) if reads else None,
    )


def test_update_aborts_when_a_process_still_holds_the_install(tmp_path, monkeypatch):
    # The service says STOPPED but a server is still running out of the install:
    # SteamCMD can't replace files it holds, and on Windows that failure is
    # silent — the old binaries survive and players get a version mismatch.
    cfg = _update_cfg(tmp_path)
    calls: list = []
    _patch_service(monkeypatch, calls)
    _patch_steamcmd(monkeypatch, calls, installed=["100", "100"])

    class _Held:
        pid = 4242

    monkeypatch.setattr(sched_mod.procs, "processes_under", lambda root: [_Held()])

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert ("update",) not in calls  # SteamCMD never ran against a locked install
    assert [c[0] for c in calls] == ["stop"]  # and the server wasn't blind-started
    assert any(
        e.kind == "error" and "still running" in e.message and "4242" in e.message
        for e in events
    )


def test_update_reports_the_build_it_landed_on(tmp_path, monkeypatch):
    cfg = _update_cfg(tmp_path)
    calls: list = []
    _patch_service(monkeypatch, calls)
    _patch_steamcmd(monkeypatch, calls, installed=["100", "200"], latest="200")
    monkeypatch.setattr(sched_mod.procs, "processes_under", lambda root: [])

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert any("Build 100 → 200" in e.message for e in events)
    assert not any(e.kind == "error" for e in events)


def test_update_flags_a_build_that_never_changed(tmp_path, monkeypatch):
    # SteamCMD exits 0 on a blocked overwrite, so the exit code is not proof.
    # Steam says 200, the disk still says 100: the update did not land, and
    # announcing success would leave players bouncing off a version mismatch.
    cfg = _update_cfg(tmp_path)
    calls: list = []
    _patch_service(monkeypatch, calls)
    _patch_steamcmd(monkeypatch, calls, installed=["100", "100"], latest="200")
    monkeypatch.setattr(sched_mod.procs, "processes_under", lambda root: [])

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert ("update",) in calls  # it ran — it just didn't take
    assert any(
        e.kind == "error" and "did NOT land" in e.message and "version mismatch" in e.message
        for e in events
    )


def test_update_says_so_when_it_cannot_verify(tmp_path, monkeypatch):
    # No manifest to read: never claim a verified update we didn't verify.
    cfg = _update_cfg(tmp_path)
    calls: list = []
    _patch_service(monkeypatch, calls)
    _patch_steamcmd(monkeypatch, calls, installed=[None, None], latest="200")
    monkeypatch.setattr(sched_mod.procs, "processes_under", lambda root: [])

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    assert any("Couldn't verify the update" in e.message for e in events)


def test_update_available_warns_once_when_the_manifest_is_unreadable(tmp_path, monkeypatch):
    # A build id that can't be read makes the update check permanently blind —
    # which is indistinguishable from "no updates" unless it says so.
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path)
    _patch_buildids(monkeypatch, installed=None, latest="200")

    bus = EventBus()
    events = _collect(bus)
    sched = sched_mod.Scheduler(cfg, FakeApi(), bus)
    assert _run(sched.check_update_available()) is False
    assert _run(sched.check_update_available()) is False

    blind = [e for e in events if e.kind == "error" and "build id" in e.message]
    assert len(blind) == 1  # said once, not every few hours


def test_update_that_resets_the_ini_restores_it_and_keeps_palctl_able_to_see(
    tmp_path, monkeypatch
):
    """The whole failure, end to end.

    A server update can leave PalWorldSettings.ini holding Palworld's defaults —
    a valid file, so the blank check never fires and the pre-update backup was
    never used. Because those defaults carry RESTAPIEnabled=False and no
    AdminPassword, palctl went permanently blind to a server that was up the
    whole time: nothing on the dashboard, and restarting fixed nothing because
    the process was never the problem.
    """
    from palctl.inifile import PalSettings

    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")
    cfg.api_port = 8212

    tuned = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ExpRate=3.000000,ServerName="mine",'
        'AdminPassword="hunter2",RESTAPIEnabled=True,RESTAPIPort=8212)\n'
    )
    defaults = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ExpRate=1.000000,ServerName="Default Palworld Server",'
        'AdminPassword="",RESTAPIEnabled=False,RESTAPIPort=8212)\n'
    )
    ini = cfg.live_ini
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(tuned, encoding="utf-8")
    cfg.default_ini.parent.mkdir(parents=True, exist_ok=True)
    cfg.default_ini.write_text(defaults, encoding="utf-8")

    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line=None):
        ini.write_text(defaults, encoding="utf-8")  # a VALID ini, just reset
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    # No keyring in tests; the admin's password has to come back from the ini.
    monkeypatch.setattr("palctl.config.get_admin_password", lambda: "")

    bus = EventBus()
    events = _collect(bus)
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server())

    after = PalSettings.load(ini)
    assert after.get("RESTAPIEnabled") is True, "palctl must still be able to see it"
    assert after.get("AdminPassword") == "hunter2", "and still authenticate"
    assert after.get("ExpRate") == 3.0, "the admin's tuning must survive an update"
    assert after.get("ServerName") == "mine"
    assert any(
        "reset" in e.message and "PalWorldSettings.ini" in e.message
        for e in events
    ), "and the admin has to be told it happened"

def test_validate_is_available_as_an_explicit_repair(tmp_path, monkeypatch):
    """Removing validate from routine updates must not remove the ability to
    repair a genuinely broken install — it just stops being the default."""
    steam = tmp_path / "steamcmd.exe"
    steam.write_bytes(b"MZ")
    cfg = Config()
    cfg.steamcmd_path = str(steam)
    cfg.server_root = str(tmp_path / "server")
    cfg.backup_root = str(tmp_path / "backups")

    calls: list = []
    _patch_service(monkeypatch, calls)

    async def fake_update(steamcmd, install_dir, *, app_id, validate, on_line=None):
        calls.append(("update", validate))
        return 0

    monkeypatch.setattr(sched_mod.steamcmd, "run_update_async", fake_update)
    monkeypatch.setattr(sched_mod.steamcmd, "backup_file", lambda p: None)

    bus = EventBus()
    _run(sched_mod.Scheduler(cfg, FakeApi(), bus).update_server(validate=True))

    assert ("update", True) in calls
