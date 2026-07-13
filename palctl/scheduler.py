"""Scheduled tasks: daily restart with countdown, autosave, periodic backup."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import backups, procs, steamcmd
from .api import PalApi
from .config import Config
from .events import Event, EventBus
from .inifile import is_blank

# Announce at these marks before a scheduled restart.
COUNTDOWN_MARKS = (600, 300, 60, 30, 10)


class Scheduler:
    def __init__(self, cfg: Config, api: PalApi, bus: EventBus) -> None:
        self._cfg = cfg
        self._api = api
        self._bus = bus

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api

    async def run(self) -> None:
        await asyncio.gather(
            self._autosave_loop(),
            self._backup_loop(),
            self._daily_restart_loop(),
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
            hours = self._cfg.schedule.backup_hours
            await asyncio.sleep(max(1, hours) * 3600)
            if not self._cfg.schedule.enabled or hours <= 0:
                continue
            await self.backup_now("scheduled")

    async def backup_now(self, label: str = "manual") -> None:
        from pathlib import Path

        try:
            # Flush the world to disk first, or the backup is a few minutes stale.
            try:
                await self._api.save()
                await asyncio.sleep(3)
            except Exception:
                pass

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
            await self._bus.emit(
                Event(
                    "backup",
                    f"📦 Backup `{b.name}` ({b.size_mb:.0f} MB)"
                    + (f", pruned {len(pruned)}" if pruned else ""),
                    {"name": b.name, "size_mb": b.size_mb},
                )
            )
        except Exception as e:
            await self._bus.emit(Event("error", f"Backup failed: {e}"))

    # ---------- daily restart ----------

    def _next_restart(self) -> datetime:
        now = datetime.now()
        try:
            hh, _, mm = self._cfg.schedule.daily_restart_at.partition(":")
            target = now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
        except ValueError:
            # A malformed time in config.json must not kill the daemon.
            target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

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

            await self.restart_with_countdown("Scheduled daily restart")

    async def restart_with_countdown(self, reason: str) -> None:
        """Announce, count down, save, restart. Also used by the GUI/bot buttons."""
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

        try:
            await self._api.save()
            await asyncio.sleep(3)
        except Exception:
            pass

        await procs.stop_service(self._cfg.service_name)
        await asyncio.sleep(3)
        await procs.start_service(self._cfg.service_name)

        ok = await self._api.wait_until_alive(timeout=240)
        await self._bus.emit(
            Event(
                "restart",
                "✅ Server back up." if ok else "❌ Server did not come back up.",
                {"recovered": ok},
            )
        )

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
        src = Path(cfg.backup_root) / name
        if any(c in name for c in ("..", "/", "\\")) or not src.is_dir():
            await self._bus.emit(Event("error", f"Restore aborted: no such backup '{name}'."))
            return

        await self._bus.emit(
            Event("restore", f"♻️ Restoring backup `{name}` — stopping the server.")
        )
        try:
            try:
                await self._api.save()
            except Exception:
                pass
            await procs.stop_service(cfg.service_name)
            await asyncio.to_thread(
                backups.restore, Path(cfg.backup_root), name, cfg.savegames_dir
            )
            await self._bus.emit(
                Event("restore", f"📥 Restored `{name}`. Starting the server.")
            )
        except Exception as e:
            await self._bus.emit(Event("error", f"Restore failed: {e}"))
        finally:
            await procs.start_service(cfg.service_name)
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

        await self._bus.emit(
            Event("update", "⏬ Server update starting — saving and stopping.")
        )
        try:
            try:
                await self._api.save()
            except Exception:
                pass
            await procs.stop_service(cfg.service_name)

            ini = cfg.live_ini
            ini_backup = await asyncio.to_thread(steamcmd.backup_file, ini)

            latest: list[str] = []

            def sink(line: str) -> None:
                if line:
                    latest.append(line)
                    del latest[:-1]  # keep only the most recent line

            code = await steamcmd.run_update_async(
                cfg.steamcmd_path,
                cfg.server_root,
                app_id=cfg.app_id,
                validate=validate,
                on_line=sink,
            )

            if ini_backup and is_blank(ini):
                await asyncio.to_thread(shutil.copy2, ini_backup, ini)
                await self._bus.emit(
                    Event(
                        "update",
                        "♻️ SteamCMD blanked PalWorldSettings.ini — restored it "
                        "from the pre-update backup.",
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
        finally:
            await procs.start_service(cfg.service_name)
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
