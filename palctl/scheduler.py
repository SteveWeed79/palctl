"""Scheduled tasks: daily restart with countdown, autosave, periodic backup."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import backups, procs, rclone, steamcmd  # noqa: F401  (procs: tests patch service ctl here)
from .api import PalApi
from .config import Config
from .control import ServerController
from .events import Event, EventBus
from .inifile import is_blank

# Announce at these marks before a scheduled restart.
COUNTDOWN_MARKS = (600, 300, 60, 30, 10)


def backup_interval_hours(raw: int) -> int:
    """Effective hours between local backups. Capped at 24 so local backups —
    the safety net — always happen at least once a day, even if a stale or
    hand-edited config asks for less. A value <= 0 is the explicit "off" escape
    hatch (not exposed in the GUI) and is preserved as-is."""
    return raw if raw <= 0 else min(24, max(1, raw))


def next_daily(now: datetime, time_str: str, fallback_hour: int = 6) -> datetime:
    """The next occurrence of HH:MM after `now`. A malformed time falls back to
    `fallback_hour`:00 rather than raising, so bad config can't kill the loop."""
    try:
        hh, _, mm = time_str.partition(":")
        target = now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
    except ValueError:
        target = now.replace(hour=fallback_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        api: PalApi,
        bus: EventBus,
        control: ServerController | None = None,
        intent_running: Callable[[], bool] | None = None,
        set_intent: Callable[[bool], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._api = api
        self._bus = bus
        # The daemon passes its shared controller so scheduled restarts,
        # updates, restores, the watchdog, and auto-recovery all serialise on
        # one lock. Standalone construction (tests) gets a private one.
        self._control = control or ServerController(cfg, api)
        # Reports whether the admin currently *wants* the server running (the
        # daemon's `_desired_running`). The time-triggered restart/update loops
        # consult it so a server deliberately stopped for maintenance is not
        # silently brought back to life at 06:00. None = always-running (tests
        # and standalone use), matching the previous behaviour.
        self._intent_running = intent_running
        # Records the admin's start/stop intent (the daemon's `_desired_running`
        # setter, which persists it). Used by start_server/stop_server so a
        # Discord /start or /stop is remembered exactly like the GUI's buttons —
        # a /stop must not be undone by auto-recovery. None = no-op (standalone).
        self._set_intent = set_intent or (lambda _running: None)

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api
        self._control.reconfigure(cfg, api)

    async def run(self) -> None:
        await asyncio.gather(
            self._autosave_loop(),
            self._backup_loop(),
            self._daily_restart_loop(),
            self._auto_update_loop(),
        )

    # ---------- autosave ----------

    async def _autosave_loop(self) -> None:
        while True:
            mins = self._cfg.schedule.autosave_minutes
            await asyncio.sleep(max(1, mins) * 60)
            if not self._cfg.schedule.enabled or mins <= 0:
                continue
            try:
                await self._api.save()
            except Exception as e:
                await self._bus.emit(Event("error", f"Autosave failed: {e}"))

    # ---------- backups ----------

    async def _backup_loop(self) -> None:
        while True:
            # Local backups run at least once a day: the interval is capped at
            # 24h so a stale or hand-edited config can't push them below the daily
            # floor the GUI enforces.
            hours = backup_interval_hours(self._cfg.schedule.backup_hours)
            await asyncio.sleep(max(1, hours) * 3600)
            if not self._cfg.schedule.enabled or hours <= 0:
                continue
            try:
                await self.backup_now("scheduled")
            except Exception as e:
                await self._bus.emit(Event("error", f"Scheduled backup failed: {e}"))

    async def backup_now(self, label: str = "manual") -> None:
        # Under the op lock: a backup mid-restore would copy a half-swapped
        # SaveGames. update/restore call _do_backup directly from inside their
        # own operation instead (the lock is not reentrant).
        async with self._control.operation("backup"):
            await self._do_backup(label)

    async def _do_backup(self, label: str = "manual") -> backups.Backup | None:
        try:
            # Flush the world to disk first, or the backup is a few minutes stale.
            await self._control.save_best_effort(settle=3)

            b = await asyncio.to_thread(
                backups.create,
                self._cfg.savegames_dir,
                Path(self._cfg.backup_root),
                label,
            )
            pruned = await asyncio.to_thread(
                backups.prune, Path(self._cfg.backup_root),
                self._cfg.schedule.backup_retain,
            )
            mirrored = await self._mirror(b)
            await self._bus.emit(
                Event(
                    "backup",
                    f"📦 Backup `{b.name}` ({b.size_mb:.0f} MB)"
                    + (f", pruned {len(pruned)}" if pruned else "")
                    + (", mirrored" if mirrored else ""),
                    {
                        "name": b.name,
                        "size_mb": b.size_mb,
                        "mirrored": mirrored,
                        "consistent": b.consistent,
                    },
                )
            )
            if not b.consistent:
                # The server wrote the world during every copy attempt, so this
                # backup's files may be from different moments. Keep it (it is
                # almost certainly fine), but say so — if someone is choosing a
                # backup to restore, a clean neighbour is the safer pick.
                await self._bus.emit(
                    Event(
                        "backup",
                        f"⚠️ Backup `{b.name}` was copied while the server was "
                        "actively writing the world, so it may be internally "
                        "inconsistent. It is kept and probably fine — but "
                        "prefer the backup before or after it for a restore.",
                    )
                )
            return b
        except Exception as e:
            await self._bus.emit(Event("error", f"Backup failed: {e}"))
            return None

    async def _mirror(self, b: backups.Backup) -> bool:
        """Second copy of the backup, if configured — onto another disk/share, or
        up to an rclone cloud remote (Google Drive, Dropbox, S3, …) when the
        mirror target is a `remote:path` string instead of a local path. A mirror
        failure must not fail the backup — the primary copy already exists."""
        root = self._cfg.backup_mirror
        if not (self._cfg.backup_mirror_enabled and root):
            return False
        # The mirror can keep a different number of copies than the local disk
        # (cloud costs money, or cold storage is cheap). 0 = match local.
        retain = self._cfg.schedule.mirror_retain or self._cfg.schedule.backup_retain
        try:
            if rclone.is_remote(root):
                await asyncio.to_thread(rclone.mirror, b.path, root)
                await asyncio.to_thread(rclone.prune, root, retain)
            else:
                await asyncio.to_thread(backups.mirror, b.path, Path(root))
                await asyncio.to_thread(backups.prune, Path(root), retain)
            return True
        except Exception as e:
            await self._bus.emit(
                Event("error", f"Backup mirror to {root} failed: {e} "
                               "(the primary backup is fine).")
            )
            return False

    # ---------- daily restart ----------

    def _intentionally_stopped(self) -> bool:
        """True when the admin has deliberately stopped the server, so a
        time-triggered restart/update must not resurrect it."""
        return self._intent_running is not None and not self._intent_running()

    def _next_restart(self) -> datetime:
        return next_daily(datetime.now(), self._cfg.schedule.daily_restart_at, 6)

    async def _daily_restart_loop(self) -> None:
        while True:
            if not (self._cfg.schedule.enabled and self._cfg.schedule.daily_restart):
                await asyncio.sleep(60)
                continue

            wait = (self._next_restart() - datetime.now()).total_seconds()
            # Wake before the restart so we can run the countdown.
            await asyncio.sleep(max(0.0, wait - COUNTDOWN_MARKS[0]))

            if not (self._cfg.schedule.enabled and self._cfg.schedule.daily_restart):
                continue
            if self._intentionally_stopped():
                await self._bus.emit(
                    Event(
                        "restart",
                        "⏸️ Skipped the scheduled daily restart — the server is "
                        "stopped on purpose. Start it and it'll resume tomorrow.",
                    )
                )
                # Don't busy-wait the mark again; wait out the countdown window.
                await asyncio.sleep(COUNTDOWN_MARKS[0])
                continue

            try:
                await self.restart_with_countdown("Scheduled daily restart")
            except Exception as e:
                await self._bus.emit(
                    Event("error", f"Scheduled daily restart failed: {e}")
                )

    # ---------- scheduled auto-update ----------

    def _next_update(self) -> datetime:
        return next_daily(datetime.now(), self._cfg.schedule.auto_update_at, 5)

    async def _auto_update_loop(self) -> None:
        while True:
            if not (self._cfg.schedule.enabled and self._cfg.schedule.auto_update):
                await asyncio.sleep(60)
                continue

            wait = (self._next_update() - datetime.now()).total_seconds()
            await asyncio.sleep(max(0.0, wait))

            if not (self._cfg.schedule.enabled and self._cfg.schedule.auto_update):
                continue
            if self._intentionally_stopped():
                await self._bus.emit(
                    Event(
                        "update",
                        "⏸️ Skipped the scheduled server update — the server is "
                        "stopped on purpose. Start it, then use Update when ready.",
                    )
                )
                await asyncio.sleep(60)
                continue

            try:
                await self.update_server()
            except Exception as e:
                await self._bus.emit(Event("error", f"Scheduled update failed: {e}"))

    async def check_update_available(self) -> bool:
        """Compare the installed build id to Steam's latest; emit an event if a
        newer one exists. Best-effort — a missing steamcmd just means 'no'."""
        cfg = self._cfg
        if not cfg.steamcmd_path or not Path(cfg.steamcmd_path).exists():
            return False
        installed = await asyncio.to_thread(
            steamcmd.installed_buildid, cfg.server_root, cfg.app_id
        )
        latest = await steamcmd.latest_buildid(cfg.steamcmd_path, cfg.app_id)
        if installed and latest and installed != latest:
            await self._bus.emit(
                Event(
                    "update_available",
                    f"⬆️ A Palworld server update is available (installed build "
                    f"{installed}, latest {latest}). Use `/update` or the Console "
                    "**Update** button when it's convenient.",
                    {"installed": installed, "latest": latest},
                )
            )
            return True
        return False

    async def restart_with_countdown(self, reason: str) -> None:
        """Announce, count down, save, restart. Also used by the GUI/bot buttons.

        Holds the op lock for the whole countdown, so an update or a watchdog
        restart can't fire into the middle of it."""
        async with self._control.operation("restart"):
            await self._bus.emit(Event("restart", f"🔁 {reason} — counting down."))

            prev = COUNTDOWN_MARKS[0]
            for mark in COUNTDOWN_MARKS:
                await asyncio.sleep(max(0, prev - mark))
                prev = mark
                label = f"{mark // 60} minute(s)" if mark >= 60 else f"{mark} seconds"
                try:
                    await self._api.announce(f"{reason} in {label}.")
                except Exception:
                    pass

            await asyncio.sleep(prev)
            await self._control.save_best_effort(settle=3)
            ok = await self._control.restart_cycle(
                escalate=True,
                on_escalate=lambda m: self._bus.emit(
                    Event("restart", f"🔨 {m}", {"action": "force_stop"})
                ),
            )
            await self._bus.emit(
                Event(
                    "restart",
                    "✅ Server back up." if ok else "❌ Server did not come back up.",
                    {"recovered": ok},
                )
            )

    async def restart_quick(self, reason: str, *, skip_if_busy: bool = False) -> None:
        """Save and restart with no countdown. For moments when there's nobody
        to warn — the leak forecaster's empty-server pre-emptive restart.

        skip_if_busy: opportunistic callers (the forecaster) must never queue
        behind another operation — control.py's own contract. By the time a
        watchdog restart releases the lock, the server was just restarted; a
        queued second restart would bounce it again for nothing."""
        if skip_if_busy:
            op = self._control.try_operation("restart")
            if op is None:
                return
        else:
            op = self._control.operation("restart")
        async with op:
            await self._bus.emit(Event("restart", f"🔁 {reason}"))
            await self._control.save_best_effort(settle=3)
            ok = await self._control.restart_cycle(
                escalate=True,
                on_escalate=lambda m: self._bus.emit(
                    Event("restart", f"🔨 {m}", {"action": "force_stop"})
                ),
            )
            await self._bus.emit(
                Event(
                    "restart",
                    "✅ Server back up." if ok else "❌ Server did not come back up.",
                    {"recovered": ok},
                )
            )

    # ---------- manual start / stop (bot & GUI parity) ----------

    async def start_server(self) -> str:
        """Start the server on an admin's request and record the intent so
        scheduling/auto-recovery treat it as 'should be up'. Mirrors the daemon's
        /action/start. Returns 'busy' if another operation holds the server,
        else 'ok'."""
        op = self._control.try_operation("start")
        if op is None:
            return "busy"
        self._set_intent(True)
        async with op:
            await self._control.start()
        return "ok"

    async def stop_server(self) -> str:
        """Save and stop the server on an admin's request, recording the Stop
        intent so auto-recovery won't resurrect it. Mirrors /action/stop.
        Returns 'busy' (another op holds it), 'ok' (confirmed STOPPED), or
        'failed' (the stop didn't confirm — likely a hung server)."""
        op = self._control.try_operation("stop")
        if op is None:
            return "busy"
        self._set_intent(False)
        async with op:
            await self._control.save_best_effort()
            ok = await self._control.stop()
        return "ok" if ok else "failed"

    # ---------- restore ----------

    async def restore_backup(self, name: str) -> None:
        """
        Stop the server, restore a backup over SaveGames, bring it back.

        `backups.restore` rejects path-traversal names and snapshots the current
        world to a `-pre-restore` copy first, so restoring the wrong one is itself
        undoable. We pre-check the name exists so a typo doesn't take the server
        down for nothing.
        """
        cfg = self._cfg
        if not backups.is_restorable(Path(cfg.backup_root), name):
            await self._bus.emit(Event("error", f"Restore aborted: no such backup '{name}'."))
            return

        async with self._control.operation("restore"):
            await self._bus.emit(
                Event("restore", f"♻️ Restoring backup `{name}` — stopping the server.")
            )
            await self._control.save_best_effort()
            if not await self._control.stop():
                # Copying over a live save corrupts it. If the server didn't
                # confirm STOPPED, refuse to touch the world at all.
                await self._bus.emit(
                    Event(
                        "error",
                        "Restore aborted: the server did not stop (it may be "
                        "hung, or the service name may be wrong). The world is "
                        "untouched. Stop the server manually, then retry.",
                    )
                )
                return
            try:
                await asyncio.to_thread(
                    backups.restore, Path(cfg.backup_root), name, cfg.savegames_dir
                )
                await self._bus.emit(
                    Event("restore", f"📥 Restored `{name}`. Starting the server.")
                )
            except Exception as e:
                await self._bus.emit(Event("error", f"Restore failed: {e}"))
            finally:
                await self._control.start()
                ok = await self._api.wait_until_alive(timeout=240)
                await self._bus.emit(
                    Event(
                        "restore",
                        "✅ Server back up after restore."
                        if ok
                        else "❌ Server did not come back after the restore. Needs a look.",
                        {"recovered": ok},
                    )
                )

    # ---------- server update (SteamCMD) ----------

    async def update_server(self, *, validate: bool = True) -> None:
        """
        Stop the server, run SteamCMD `app_update`, and bring it back — the thing
        that finally uses the steamcmd_path / app_id the config always stored.

        The `validate` pass is what blanks PalWorldSettings.ini, so we copy the
        ini aside first and, if Steam does wipe it, put it straight back. Losing
        an afternoon of server tuning to an update is the exact papercut this
        avoids.
        """
        cfg = self._cfg
        steam = Path(cfg.steamcmd_path)
        if not cfg.steamcmd_path or not steam.exists():
            await self._bus.emit(
                Event(
                    "error",
                    "Can't update: steamcmd.exe isn't set or doesn't exist. "
                    "Set its path in Config (there's an Auto-detect button).",
                )
            )
            return

        async with self._control.operation("update"):
            await self._update_locked(cfg, validate=validate)

    async def _update_locked(self, cfg: Config, *, validate: bool) -> None:
        await self._bus.emit(
            Event("update", "⏬ Server update starting — backing up, saving, stopping.")
        )
        # Game updates are exactly when save migration or corruption
        # happens; a world backup first makes a bad update undoable. A fresh
        # install with no SaveGames yet has nothing to protect (same rule the
        # wizard uses), so only a world that exists gates the update.
        if not cfg.savegames_dir.exists():
            await self._bus.emit(
                Event(
                    "update",
                    "No world to back up yet (SaveGames doesn't exist) — "
                    "skipping the pre-update backup.",
                )
            )
        else:
            b = await self._do_backup("pre-update")
            if b is None:
                if cfg.schedule.update_requires_backup:
                    await self._bus.emit(
                        Event(
                            "error",
                            "Update aborted: the pre-update backup failed (see "
                            "the error above), so a bad update could not be "
                            "rolled back. Nothing was changed. Fix the backup "
                            "problem (disk space? backup folder path?) and "
                            "retry — or untick 'Update requires a backup' in "
                            "Config to proceed without a safety net.",
                        )
                    )
                    return
                await self._bus.emit(
                    Event(
                        "update",
                        "⚠️ Pre-update backup failed (see the error above) — "
                        "continuing with the update anyway.",
                    )
                )
        if not await self._control.stop():
            # SteamCMD rewriting the install under a still-running server
            # corrupts it. If the server didn't confirm STOPPED, don't update.
            await self._bus.emit(
                Event(
                    "error",
                    "Update aborted: the server did not stop (it may be hung, "
                    "or the service name may be wrong). Nothing was changed. "
                    "Stop the server manually, then retry.",
                )
            )
            return
        try:
            ini = cfg.live_ini
            ini_backup = await asyncio.to_thread(steamcmd.backup_file, ini)

            latest: list[str] = []

            def sink(line: str) -> None:
                if line:
                    latest.append(line)
                    del latest[:-1]  # keep only the most recent line

            try:
                code = await steamcmd.run_update_async(
                    cfg.steamcmd_path,
                    cfg.server_root,
                    app_id=cfg.app_id,
                    validate=validate,
                    on_line=sink,
                )
            finally:
                # SteamCMD can blank the ini and then die — put the settings
                # back even on failure, before the server is started again.
                if ini_backup and is_blank(ini):
                    await asyncio.to_thread(shutil.copy2, ini_backup, ini)
                    await self._bus.emit(
                        Event(
                            "update",
                            "♻️ SteamCMD blanked PalWorldSettings.ini — restored "
                            "it from the pre-update backup.",
                        )
                    )

            tail = f" ({latest[0]})" if latest else ""
            await self._bus.emit(
                Event(
                    "update",
                    (f"✅ SteamCMD finished (exit {code}).{tail}" if code == 0
                     else f"⚠️ SteamCMD exited {code}.{tail}") + " Starting server.",
                    {"exit_code": code},
                )
            )
        except Exception as e:
            # Without this, a GUI- or bot-triggered update that throws would
            # restart the server and announce success with no trace of the
            # failure (only the scheduled path had a catch).
            await self._bus.emit(Event("error", f"Update failed: {e}"))
        finally:
            await self._control.start()
            ok = await self._api.wait_until_alive(timeout=300)
            await self._bus.emit(
                Event(
                    "update",
                    "✅ Server back up after update."
                    if ok
                    else "❌ Server did not come back after the update. Needs a look.",
                    {"recovered": ok},
                )
            )
