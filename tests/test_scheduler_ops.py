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
    async def save(self):
        pass

    async def wait_until_alive(self, timeout=240):
        return True


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
    # the update ran against the configured install dir and app id, with validate
    assert calls[1][1] == cfg.server_root and calls[1][2] == cfg.app_id and calls[1][3] is True
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
    cfg = Config()
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
    cfg = Config()
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
