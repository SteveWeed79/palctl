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
