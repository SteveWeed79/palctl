"""Scheduled tasks: daily restart with countdown, autosave, periodic backup."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

# procs is imported as a module, not by name: the tests patch service control
# through this reference.
from . import backups, countdown, procs, rclone, steamcmd
from .api import PalApi
from .config import Config
from .control import ServerController
from .countdown import Countdown
from .events import Event, EventBus
from .inifile import is_blank

# The emoji each countdown opens with, so the event feed reads the same as the
# operation it precedes.
_COUNTDOWN_ICON = {"restart": "🔁", "restore": "♻️"}

# Where an admin can reach the two escape hatches. Repeated in the opening
# event because the old countdown announced `/cancel` — a Discord command that
# does not exist on the many installs running with the bot switched off.
_ESCAPES = (
    "Cancel it or skip the wait from the dashboard, the Console tab, "
    "`palctl cancel` / `palctl skip`, or Discord `/cancel` / `/now`."
)

# The operations that run a countdown, and so have a window an admin can arrive
# too late for. Every *other* operation — a backup, an update, a boot-time
# start, a watchdog restart that handed its timer to the game — never had one,
# so "too late" would be the wrong word: there was nothing to be in time for.
_COUNTDOWN_OPS = frozenset({"restart", "restore"})


def _dir_size_bytes(path: Path | str) -> int:
    """Total size of a directory tree. Blocking — call via to_thread. Best-effort:
    unreadable entries are skipped, a missing dir is 0."""
    total = 0
    try:
        for p in Path(path).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _free_bytes(path: Path | str) -> int | None:
    """Free bytes on the volume holding `path`, or None if unreadable.
    Blocking — call via to_thread."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


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


def next_restart_target(now: datetime, every_hours: int, time_str: str) -> datetime:
    """When the next scheduled restart is due. every_hours > 0 = interval mode
    (every N hours from now, clamped to at least 1h so a hand-edited config
    can't tight-loop restarts); otherwise the classic daily-at-HH:MM."""
    if every_hours > 0:
        return now + timedelta(hours=max(1, every_hours))
    return next_daily(now, time_str, 6)


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
        # The countdown currently running ahead of a restart or a restore, or
        # None. Every surface reads it (`/state`), and every surface can cancel
        # it or cut it short — it used to be a bare Event that only Discord's
        # /cancel could reach, and nothing at all could shorten.
        self._countdown: Countdown | None = None
        # One-shot: the update check runs every few hours, and a server root with
        # no readable Steam manifest would otherwise repeat the same warning
        # forever. Reset on reconfigure, so fixing the path re-arms it.
        self._manifest_warned = False
        # The standing answer to "is this server on the build Steam is serving
        # clients?", refreshed by every update check and surfaced on /state.
        # Kept because the mismatch it catches is the one failure players hit
        # first: they're refused at the join screen while palctl — correctly —
        # reports a healthy server.
        self.update_status: dict = {"state": "unknown", "checked_at": None}

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        self._cfg = cfg
        self._api = api
        self._control.reconfigure(cfg, api)
        # A reload is how someone fixes a wrong server root; let the manifest
        # warning fire again against the new one instead of staying quiet.
        self._manifest_warned = False

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

            # Don't start a backup the disk can't hold. A copy that fills the
            # volume mid-write leaves a corrupt backup AND breaks the live world's
            # next save. Skip loudly instead — the whole point of backups is to be
            # there when needed, so a silent out-of-space failure is the worst case.
            need = await asyncio.to_thread(_dir_size_bytes, self._cfg.savegames_dir)
            free = await asyncio.to_thread(_free_bytes, self._cfg.backup_root)
            if need and free is not None and free < need * 1.2:
                await self._bus.emit(
                    Event(
                        "error",
                        f"Backup skipped: only {free / 1e9:.1f} GB free on the "
                        f"backup volume but the world is ~{need / 1e9:.1f} GB. Free "
                        "some space — backups are failing.",
                        {"free_gb": round(free / 1e9, 1), "need_gb": round(need / 1e9, 1)},
                    )
                )
                return None

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

            # Interval mode (every N hours) or daily-at-a-time — re-read each
            # cycle so a config reload switches mode on the next lap.
            target = next_restart_target(
                datetime.now(),
                self._cfg.schedule.restart_every_hours,
                self._cfg.schedule.daily_restart_at,
            )
            wait = (target - datetime.now()).total_seconds()
            # Wake a full countdown before the target so the countdown *ends* on
            # it. The lead is whatever the countdown is configured to be, not a
            # hard-coded ten minutes.
            lead = countdown.clamp_seconds(self._cfg.schedule.restart_countdown_seconds)
            await asyncio.sleep(max(0.0, wait - lead))

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
            else:
                # An empty server collapses the countdown to a few seconds, and
                # a restart that then ran immediately would happen a full lead
                # time *before* the hour the admin scheduled. Spend the
                # difference waiting instead, so 06:00 still means 06:00.
                effective, _why = await self._effective_countdown(lead)
                await asyncio.sleep(max(0.0, lead - effective))
                try:
                    await self.restart_with_countdown(
                        "Scheduled daily restart", seconds=lead
                    )
                except Exception as e:
                    await self._bus.emit(
                        Event("error", f"Scheduled daily restart failed: {e}")
                    )

            # Advance past today's target before recomputing, or a restart that
            # finished (or a /cancel that returned) while `now` is still before
            # the target would resolve _next_restart() to *today* again and
            # re-fire immediately. A successful restart already runs past the
            # target (the countdown takes minutes); a cancel or a skip needs this
            # to wait the window out so /cancel actually skips the day.
            remaining = (target - datetime.now()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining + 1)

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
        newer one exists. Best-effort — a missing steamcmd just means 'no'.

        The answer is also *kept* (`update_status`), not only announced. A
        version mismatch is the one failure players hit before the admin does —
        they're refused at the join screen while every palctl surface says the
        server is healthy, because it is. An event scrolls away; a standing
        "this server is N builds behind" is what turns that support call into a
        glance at the dashboard.
        """
        cfg = self._cfg
        # Off the loop like every other filesystem touch here: steamcmd_path is
        # user-supplied and is sometimes a UNC path, where a stat against a dead
        # share blocks for the SMB timeout rather than microseconds.
        if not cfg.steamcmd_path or not await asyncio.to_thread(
            Path(cfg.steamcmd_path).exists
        ):
            self._record_update_status(state="unknown", detail="no steamcmd configured")
            return False
        installed = await asyncio.to_thread(
            steamcmd.installed_buildid, cfg.server_root, cfg.app_id
        )
        if installed is None:
            self._record_update_status(
                state="unknown", detail="Steam's appmanifest could not be read"
            )
            # Nothing to compare against: this check is now permanently blind,
            # so the first sign of a server left behind on an old build would be
            # players bouncing off the join screen. Say so once per daemon run.
            await self._warn_unreadable_manifest()
            return False
        latest = await steamcmd.latest_buildid(cfg.steamcmd_path, cfg.app_id)
        if not latest:
            self._record_update_status(
                state="unknown", installed=installed,
                detail="Steam didn't report a latest build",
            )
            return False
        self._record_update_status(
            state="behind" if installed != latest else "current",
            installed=installed, latest=latest,
        )
        if latest and installed != latest:
            await self._bus.emit(
                Event(
                    "update_available",
                    f"⬆️ A Palworld server update is available (installed build "
                    f"{installed}, latest {latest}). Once Steam updates a "
                    "player's game client, they'll be refused with a **version "
                    "mismatch** until the server is on the same build — so don't "
                    "leave this long. Use `/update` or the Console **Update** "
                    "button.",
                    {"installed": installed, "latest": latest},
                )
            )
            return True
        return False

    def _record_update_status(
        self,
        *,
        state: str,
        installed: str | None = None,
        latest: str | None = None,
        detail: str = "",
    ) -> None:
        """state: current | behind | unknown."""
        import time as _time

        self.update_status = {
            "state": state,
            "installed": installed,
            "latest": latest,
            "detail": detail,
            "checked_at": _time.time(),
        }

    async def _warn_unreadable_manifest(self) -> None:
        """One warning per daemon run when Steam's appmanifest can't be found
        under the configured server root. Without it, update detection fails
        silently and looks exactly like 'there are no updates'."""
        if self._manifest_warned:
            return
        self._manifest_warned = True
        await self._bus.emit(
            Event(
                "error",
                f"Can't read the installed Palworld build id: no "
                f"`appmanifest_{self._cfg.app_id}.acf` under "
                f"`{self._cfg.server_root}` or its Steam library. palctl can't "
                "tell you when a server update lands, so the first sign will be "
                "players getting a **version mismatch**. Check that Server root "
                "in Config points at the install the server actually runs from.",
                {"server_root": str(self._cfg.server_root)},
            )
        )

    # ---------- the countdown, and the two ways out of it ----------

    def cancel_countdown(self) -> str:
        """Abort the countdown in flight, so the operation never happens.

        Returns one of:
          "cancelled" — done; the server stays up.
          "too_late"  — a restart or restore is running and its countdown is
                        over, so it can no longer be called off.
          "idle"      — no countdown to interrupt. Includes a server busy with
                        something that never had one (a backup, an update, a
                        boot-time start): "too late" would imply the admin
                        nearly made a window that never existed.

        Three outcomes rather than a bool because the failures need different
        words: an admin who clicked Cancel two seconds late is not told
        "nothing was running", which reads as a broken button.
        """
        cd = self._countdown
        if cd is not None and cd.cancel():
            return "cancelled"
        return self._missed_verdict(cd)

    def skip_countdown(self) -> str:
        """Stop waiting and run the operation now. Same three outcomes as
        cancel_countdown(), with "skipped" in place of "cancelled".

        The half that was missing entirely: an admin who knows the server is
        empty had no way to say so, and paid the full countdown for it."""
        cd = self._countdown
        if cd is not None and cd.skip():
            return "skipped"
        return self._missed_verdict(cd)

    def _missed_verdict(self, cd: Countdown | None) -> str:
        """Why cancel/skip couldn't do anything: "too_late" or "idle".

        Deliberately narrower than "is the server busy?". The daemon starts the
        server at boot, backs up on a schedule and updates on another — none of
        which an admin can be late for, and all of which would otherwise answer
        a Cancel with "too late", sending them looking for a countdown that
        never existed."""
        if cd is not None:
            return "too_late"  # it exists but already has a verdict, or has run out
        return "too_late" if self._control.current_op in _COUNTDOWN_OPS else "idle"

    def countdown_state(self) -> dict | None:
        """The countdown as `/state` publishes it, or None. Every client draws
        its clock and its Cancel / Now buttons from this one reading."""
        cd = self._countdown
        return cd.state() if cd is not None else None

    async def _players_to_warn(self) -> int | None:
        """How many players would actually see an in-game countdown.

        0 means nobody is on. None means the REST API didn't answer — in which
        case an announcement reaches nobody either, so for the purpose of
        "is this wait buying anything?" the two are the same answer."""
        try:
            return len(await self._api.players())
        except Exception:
            return None

    async def _effective_countdown(self, requested: int) -> tuple[int, str]:
        """(seconds, why-it-was-shortened) for a countdown of `requested`.

        Waiting ten minutes to warn an empty server is the single biggest
        source of "palctl made me wait for nothing". Collapse it — unless the
        admin has turned that off, or there are players who deserve the notice.
        """
        total = countdown.clamp_seconds(requested)
        if total <= countdown.EMPTY_SERVER_SECONDS:
            return total, ""
        if not self._cfg.schedule.skip_countdown_when_empty:
            return total, ""
        online = await self._players_to_warn()
        if online:
            return total, ""
        if online == 0:
            return countdown.EMPTY_SERVER_SECONDS, "nobody is online to warn"
        return countdown.EMPTY_SERVER_SECONDS, (
            "the server's REST API isn't answering, so an in-game warning "
            "would reach nobody"
        )

    async def _count_down(
        self, kind: str, reason: str, requested: int, *, announce_as: str | None = None
    ) -> bool:
        """Run the pre-operation countdown. True = go ahead, False = cancelled.

        Registers the countdown on `self._countdown` for its whole life, which
        is what makes it visible on `/state` and reachable by cancel/skip from
        every surface. `announce_as` is the player-facing wording where the
        operator's is more specific than players need."""
        total, why = await self._effective_countdown(requested)
        icon = _COUNTDOWN_ICON.get(kind, "⏳")
        if total <= 0:
            await self._bus.emit(
                Event(kind, f"{icon} {reason} — no countdown, going now.",
                      {"countdown_seconds": 0})
            )
            return True

        await self._bus.emit(
            Event(
                kind,
                f"{icon} {reason} — {countdown.humanize(total)} to go"
                + (f" ({why})" if why else "")
                + f". {_ESCAPES}",
                {"countdown_seconds": total, "shortened": why},
            )
        )
        cd = Countdown(
            kind,
            reason,
            total,
            announce_as=announce_as,
            # Resolved per announcement, not bound now: a countdown outlives a
            # config reload, and Countdown treats a failure here as "nobody
            # heard it" rather than a reason to stop counting down.
            announce=lambda m: self._api.announce(m),
            notify=lambda m: self._bus.emit(Event(kind, m)),
        )
        self._countdown = cd
        try:
            return await cd.run()
        finally:
            self._countdown = None

    async def restart_with_countdown(
        self, reason: str, *, seconds: int | None = None
    ) -> bool:
        """Announce, count down, save, restart. Also used by the GUI/bot buttons.

        Holds the op lock for the whole countdown, so an update or a watchdog
        restart can't fire into the middle of it. `seconds` overrides the
        configured countdown for this one restart (0 = go now, no warning);
        None uses `schedule.restart_countdown_seconds`.

        Returns True if the restart ran — including when an admin cut the
        countdown short — and False if it was cancelled. The scheduled-restart
        loop needs to tell those apart so a cancel skips today's slot instead of
        re-arming it."""
        async with self._control.operation("restart"):
            # A restart intends the server up afterward — record that so it has
            # parity with the daemon's HTTP /action/restart and a prior /stop
            # can't leave auto-recovery thinking the server should stay down.
            self._set_intent(True)
            requested = (
                self._cfg.schedule.restart_countdown_seconds
                if seconds is None
                else seconds
            )
            if not await self._count_down("restart", reason, requested):
                return False
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
            return True

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
            self._set_intent(True)  # ends with the server up — record that intent
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

    # ---------- fire-and-forget reservation (bot /restart, /update) ----------

    @property
    def current_op(self) -> str | None:
        """Name of the operation currently holding (or reserved on) the server,
        or None. Lets a caller tell the user *why* the server is busy."""
        return self._control.current_op

    def reserve(self, name: str) -> bool:
        """Synchronously claim the server for a fire-and-forget operation the
        caller is about to spawn as a task (the Discord bot's /restart and
        /update, which post 'I'll report back' and run detached). Returns False
        if something already holds or reserved it, so the caller can report
        'busy' instead of silently queueing a second countdown behind the first
        — matching the daemon's HTTP /action path. Pair with clear_reservation()
        in the spawned task's finally; operation() clears the reservation itself
        the moment it takes the real lock."""
        return self._control.reserve(name)

    def clear_reservation(self, name: str) -> None:
        self._control.clear_reservation(name)

    # ---------- restore ----------

    async def restore_backup(self, name: str, *, seconds: int | None = None) -> bool:
        """
        Warn players, stop the server, restore a backup over SaveGames, bring
        it back. Returns True if the restore ran, False if it was refused or
        cancelled.

        `backups.restore` rejects path-traversal names and snapshots the current
        world to a `-pre-restore` copy, so restoring the wrong one is itself
        undoable. We pre-check the name exists so a typo doesn't take the server
        down for nothing.

        The countdown is the part that used to be missing: a restore dropped
        everyone the instant the button was clicked, which is both rude to
        players and left no window in which to take back a mis-click. `seconds`
        overrides it for this one restore (0 = the old immediate behaviour);
        None uses `schedule.restore_countdown_seconds`.
        """
        cfg = self._cfg
        if not backups.is_restorable(Path(cfg.backup_root), name):
            await self._bus.emit(Event("error", f"Restore aborted: no such backup '{name}'."))
            return False

        async with self._control.operation("restore"):
            self._set_intent(True)  # the server is meant to be up after a restore
            requested = (
                cfg.schedule.restore_countdown_seconds if seconds is None else seconds
            )
            if not await self._count_down(
                "restore",
                f"Restoring backup {name}",
                requested,
                # Players get told the server is going down, not the name of a
                # timestamped folder on the admin's disk.
                announce_as="Server restart to restore a backup",
            ):
                return False

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
                return False
            if not await self._confirm_world_is_free(cfg):
                return False

            try:
                warning = await asyncio.to_thread(
                    backups.restore, Path(cfg.backup_root), name, cfg.savegames_dir
                )
                await self._bus.emit(
                    Event("restore", f"📥 Restored `{name}`. Starting the server.")
                )
                if warning:
                    await self._bus.emit(Event("error", warning))
            except Exception as e:
                await self._bus.emit(Event("error", f"Restore failed: {e}"))
                if not await asyncio.to_thread(cfg.savegames_dir.exists):
                    # The world is not there. Starting the server now would have
                    # Palworld generate a brand-new one over the top of the
                    # problem — players would join it, build in it, and the real
                    # world would become un-mergeable. Leave the server down and
                    # say exactly where both copies are instead.
                    await self._bus.emit(
                        Event(
                            "error",
                            "Restore failed with no world in place, so the server "
                            "was NOT started — starting it would generate an empty "
                            f"world over the problem. Look in `{cfg.savegames_dir.parent}` "
                            f"for `{cfg.savegames_dir.name}.partial-restore` (the backup "
                            f"being restored) and `{cfg.savegames_dir.name}.pre-restore` "
                            f"(the world as it was), and in `{cfg.backup_root}` for the "
                            "`-pre-restore` copies. Put one of them back as "
                            f"`{cfg.savegames_dir.name}`, then start the server.",
                            {"savegames": str(cfg.savegames_dir)},
                        )
                    )
                    return False

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
            return True

    async def _confirm_world_is_free(self, cfg: Config) -> bool:
        """
        The service says STOPPED — but is anything still holding the world open?

        The same hazard the update path checks for (a hung shutdown, or a
        leftover second service pointed at the same folder), with a different
        consequence: a live PalServer writes .sav files while the restore swaps
        them, and on Windows it also locks them so the swap fails halfway. The
        update path has guarded against this since the version-mismatch bug;
        restore — which overwrites the world itself — did not.
        """
        pids = await self._live_server_pids(cfg)
        if not pids:
            return True
        await self._bus.emit(
            Event(
                "error",
                "Restore aborted: the service reports STOPPED but a Palworld "
                f"server is still running from `{cfg.server_root}` (PID "
                f"{', '.join(str(p) for p in pids)}). Restoring over a world that "
                "process still holds open corrupts it. Nothing was changed. End "
                "that process (and check for a second, leftover server service), "
                "then retry.",
                {"pids": pids, "server_root": str(cfg.server_root)},
            )
        )
        return False

    async def _live_server_pids(self, cfg: Config) -> list[int]:
        """PIDs of Palworld server processes still running out of the install."""
        alive = await asyncio.to_thread(procs.processes_under, cfg.server_root)
        return [p.pid for p in alive]

    # ---------- server update (SteamCMD) ----------

    async def update_server(self, *, validate: bool = False) -> None:
        """
        Stop the server, run SteamCMD `app_update`, and bring it back — the thing
        that finally uses the steamcmd_path / app_id the config always stored.

        ``validate`` is **off** for routine updates, and that is a deliberate
        change from how this used to work. Every update — scheduled, GUI button,
        Discord `/update` — used to run `app_update … validate`, and `validate`
        is not an update: it is a full checksum of every file in the install
        against Steam's manifest, restoring anything that differs. Valve's own
        guidance is to use it to repair a suspected-broken install, not to
        update one. Running it routinely cost a full multi-GB verification pass
        on every single update, and — as this module's old docstring cheerfully
        admitted — it is the thing that resets PalWorldSettings.ini. palctl was
        causing that damage itself, on a schedule, and then trying to undo it
        afterwards.

        Plain `app_update` still installs the newest build; it just doesn't
        re-verify files that were already correct. Pass ``validate=True`` to get
        the old behaviour deliberately, as a repair.
        """
        cfg = self._cfg
        steam = Path(cfg.steamcmd_path)
        # to_thread for the same reason as check_update_available: a UNC path on
        # a dead share turns this stat into an event-loop stall.
        if not cfg.steamcmd_path or not await asyncio.to_thread(steam.exists):
            await self._bus.emit(
                Event(
                    "error",
                    "Can't update: steamcmd.exe isn't set or doesn't exist. "
                    "Set its path in Config (there's an Auto-detect button).",
                )
            )
            return

        async with self._control.operation("update"):
            self._set_intent(True)  # the server is meant to be up after an update
            await self._update_locked(cfg, validate=validate)

    async def _heal_ini_after_update(
        self, cfg: Config, ini: Path, ini_backup: Path | None
    ) -> None:
        """Put PalWorldSettings.ini back the way the admin had it, and make sure
        palctl can still reach the server through it.

        A server update can leave the ini blank, or — the case that used to slip
        through entirely — full of Palworld's *defaults*. The second is worse
        precisely because it looks fine: `is_blank` says no, so the pre-update
        backup was never used, every tuned rate silently reverted, and since the
        defaults carry `RESTAPIEnabled=False` and no `AdminPassword`, palctl went
        permanently blind to a server that was running the whole time. Nothing in
        the daemon could see it, restarting fixed nothing (the process was never
        the problem), and the only clue was "the server did not come back".

        So: restore a blank file wholesale, merge the admin's own values back
        over a reset one, then re-assert the three REST API settings palctl
        needs — the same call setup makes, which is idempotent and until now was
        never made again for the life of the install."""
        from .config import get_admin_password
        from .serversetup import ensure_rest_api, restore_user_settings

        if ini_backup and await asyncio.to_thread(is_blank, ini):
            await asyncio.to_thread(shutil.copy2, ini_backup, ini)
            await self._bus.emit(
                Event(
                    "update",
                    "♻️ The update blanked PalWorldSettings.ini — restored it "
                    "from the pre-update backup.",
                )
            )
        elif ini_backup:
            restored = await asyncio.to_thread(restore_user_settings, ini, ini_backup)
            if restored:
                shown = ", ".join(f"`{k}`" for k in restored[:8])
                more = f" (+{len(restored) - 8} more)" if len(restored) > 8 else ""
                await self._bus.emit(
                    Event(
                        "update",
                        f"♻️ The update reset {len(restored)} setting(s) in "
                        f"PalWorldSettings.ini back to Palworld's defaults — put "
                        f"your values back: {shown}{more}. The pre-update copy is "
                        f"at `{ini_backup.name}` if you want to compare.",
                        {"restored": restored, "backup": str(ini_backup)},
                    )
                )

        # Re-assert: cheap, idempotent, and the one thing that decides whether
        # palctl can still see the server it just updated.
        #
        # Skipped only when there is no ini and no default to seed one from —
        # that means the server isn't installed, which the update's own "did the
        # files arrive?" reporting already covers. Warning again here would just
        # add noise to a failure the caller has already explained.
        have_ini = await asyncio.to_thread(ini.exists)
        have_default = await asyncio.to_thread(cfg.default_ini.exists)
        if not have_ini and not have_default:
            return

        password = await asyncio.to_thread(get_admin_password)
        try:
            await asyncio.to_thread(
                ensure_rest_api,
                ini,
                cfg.default_ini,
                port=cfg.api_port,
                password=password,
            )
        except Exception as e:
            await self._bus.emit(
                Event(
                    "error",
                    "⚠️ Couldn't confirm the REST API settings in "
                    f"PalWorldSettings.ini after the update ({e}). If palctl "
                    "can't see the server from here, check RESTAPIEnabled, "
                    "RESTAPIPort and AdminPassword in that file.",
                )
            )

    async def _confirm_install_is_free(self, cfg: Config) -> bool:
        """
        The service says STOPPED — but is the install actually free to rewrite?

        A hung shutdown, or a leftover second service pointed at the same folder,
        can leave a PalServer process alive after the service manager has moved
        on. SteamCMD can't replace files that process holds open, and on Windows
        it fails that overwrite quietly: the update "succeeds", the old binaries
        survive, and the first anyone hears of it is players being refused with a
        version mismatch. Abort with the cause instead — the world is untouched
        and the server is down, which is a state a human can act on.
        """
        alive = await self._live_server_pids(cfg)
        if not alive:
            return True
        pids = ", ".join(str(p) for p in alive)
        await self._bus.emit(
            Event(
                "error",
                f"Update aborted: the service reports STOPPED but a Palworld "
                f"server is still running from `{cfg.server_root}` (PID {pids}). "
                "SteamCMD can't replace files a live process holds — it would "
                "half-apply the update and leave the old binaries in place, "
                "which players meet as a **version mismatch**. Nothing was "
                "changed. End that process (and check for a second, leftover "
                "server service in services.msc), then retry.",
                {"pids": alive, "server_root": str(cfg.server_root)},
            )
        )
        return False

    async def _verify_update_landed(
        self, cfg: Config, *, before: str | None, after: str | None
    ) -> bool:
        """
        Confirm the build on disk is really Steam's latest, instead of trusting
        the exit code.

        SteamCMD's exit status is not proof: it exits 0 for "nothing to do", and
        a blocked overwrite (locked file, full disk, a wrong ``force_install_dir``
        that quietly installed somewhere else) can still finish tidily. Reporting
        "✅ back up" on that leaves the server on the old build — the exact
        failure players see as a version mismatch — so the build id gets checked
        against Steam before we claim the update worked.

        Best-effort by design: when Steam can't be reached, or the manifest can't
        be read, we say what we couldn't verify rather than invent a verdict.
        """
        if after is None:
            # Can't verify. Not an error in itself (the periodic check already
            # reports an unreadable manifest as a config problem), but never
            # claim a verified update we didn't verify.
            await self._bus.emit(
                Event(
                    "update",
                    f"⚠️ Couldn't verify the update: no "
                    f"`appmanifest_{cfg.app_id}.acf` under `{cfg.server_root}`, "
                    "so palctl can't confirm which build is now installed. If "
                    "players hit a version mismatch, check that Server root "
                    "points at the install SteamCMD is writing to.",
                    {"server_root": str(cfg.server_root)},
                )
            )
            return False
        latest = await steamcmd.latest_buildid(cfg.steamcmd_path, cfg.app_id)
        if not latest:
            return False  # offline / steamcmd trouble: nothing to compare against
        if after == latest:
            return True
        await self._bus.emit(
            Event(
                "error",
                f"⚠️ The update did NOT land: the server is still on build "
                f"{after} but Steam's latest is {latest}"
                + (" (unchanged by this update)." if before == after else ".")
                + " Players whose game client has updated will be refused with a "
                "**version mismatch**. The usual causes: a file was locked by a "
                "still-running server or a second server service; the disk is "
                "full; or Server root in Config points at a different install "
                "than the one the service actually starts. Fix that and run the "
                "update again.",
                {"installed": after, "latest": latest, "build_before": before},
            )
        )
        return False

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
        if not await self._confirm_install_is_free(cfg):
            return
        try:
            ini = cfg.live_ini
            # What's on disk before SteamCMD touches it, so the update can be
            # verified afterwards rather than trusted (see _verify_update_landed).
            before = await asyncio.to_thread(
                steamcmd.installed_buildid, cfg.server_root, cfg.app_id
            )
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
                # Whatever the update did to the ini, put it right before the
                # server comes back up. Runs on failure too — a SteamCMD that
                # died halfway is exactly when the ini is half-rewritten.
                await self._heal_ini_after_update(cfg, ini, ini_backup)

            tail = f" ({latest[0]})" if latest else ""
            after = await asyncio.to_thread(
                steamcmd.installed_buildid, cfg.server_root, cfg.app_id
            )
            build = ""
            if after and after != before:
                build = f" Build {before or 'unknown'} → {after}."
            elif after:
                build = f" Build {after} (unchanged)."
            await self._bus.emit(
                Event(
                    "update",
                    (f"✅ SteamCMD finished (exit {code}).{tail}" if code == 0
                     else f"⚠️ SteamCMD exited {code}.{tail}")
                    + build + " Starting server.",
                    {"exit_code": code, "build_before": before, "build_after": after},
                )
            )
            await self._verify_update_landed(cfg, before=before, after=after)
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
