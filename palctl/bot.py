"""
Discord bot.

Self-hosted, your own token, your own box. No cloud bridge, no subscription.

Read this before you compare it to your Hytale bot: there is NO chat relay, and
there can't be. Palworld exposes no chat-read endpoint and dedicated servers
ship no log file. Chat is UE4SS-only territory.

What you DO get, all reconstructed from polling /players and /metrics:
  reads:   /status /health /players /whois /playtime /leaderboard /backups
           /events /next /help
  admin:   /start /stop /restart /cancel /update /save /backup /restore
           /announce /kick /ban /unban
  alerts:  join + leave, level-up, watchdog, up/down, update-available

/playtime and /whois answer for offline players too (resolved from the session
history palctl keeps). Player-name and backup-name arguments autocomplete, and
the destructive commands (/stop /update /restore) ask for a button confirmation
first — all so the bot is safe to drive one-handed from a phone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands

from . import backups, leak, procs
from .api import PalApi, PalApiError
from .config import Config, get_discord_token
from .events import Event, EventBus, SessionStore

BLUE = 0x47C8FF
RED = 0xF85149
GREEN = 0x3FB950


def _admin_allowed(
    admin_id: int,
    caller_id: int,
    caller_role_ids: set[int],
    has_manage_guild: bool,
) -> bool:
    """Decide whether a caller may run admin commands.

    `admin_id` is a Discord snowflake from config. Snowflakes carry no type
    tag, so a role ID and a user ID are indistinguishable as bare numbers —
    and people routinely paste the wrong one. We therefore accept *either*:
    the caller holds a role with that ID, or the caller *is* that user. A
    typo'd ID simply matches neither and denies, which is the safe default.

    `admin_id == 0` means nothing is configured -> fall back to the guild's
    Manage Server permission.
    """
    if not admin_id:
        return has_manage_guild
    return caller_id == admin_id or admin_id in caller_role_ids


def _fmt_uptime(seconds: float) -> str:
    d = timedelta(seconds=int(seconds))
    h, rem = divmod(d.seconds, 3600)
    m = rem // 60
    return f"{d.days}d {h}h {m}m" if d.days else f"{h}h {m}m"


def _fmt_last_seen(iso: str) -> str:
    """A session's ISO `left_at` as a readable UTC stamp for /whois."""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return "unknown"


# Event kinds safe to show any member via /events. Everything else — notably
# "error" (raw exceptions, cloud-remote paths) and "watchdog" internals — is
# admins-only, so a public /events can't leak operator diagnostics.
_PUBLIC_EVENT_KINDS = frozenset({
    "join", "leave", "levelup", "server_up", "server_down",
    "restart", "backup", "restore", "update", "update_available",
})


def _visible_events(events: list, is_admin: bool) -> list:
    """Filter the event feed for /events: admins see everything, everyone else
    sees only non-sensitive kinds (no raw error/watchdog internals)."""
    if is_admin:
        return list(events)
    return [e for e in events if e.kind in _PUBLIC_EVENT_KINDS]


def _match_choices(options: list[str], current: str, limit: int = 25) -> list[str]:
    """Filter `options` for a slash-command autocomplete box: prefix matches
    first, then substring matches, capped at `limit` (Discord shows at most 25).
    Pure, so the ranking is unit-tested without a Discord connection."""
    cur = current.strip().lower()
    if not cur:
        picked = options
    else:
        prefix = [o for o in options if o.lower().startswith(cur)]
        contains = [o for o in options if cur in o.lower() and not o.lower().startswith(cur)]
        picked = prefix + contains
    # Dedup identical strings (Palworld names aren't unique — two "Bob"s online
    # would otherwise yield two identical Choices, which Discord rejects), while
    # preserving the prefix-before-contains order.
    return list(dict.fromkeys(picked))[:limit]


def _health_fields(
    *,
    memory_mb: float,
    limit_mb: int,
    cpu: float,
    fps: int,
    frame_time: float,
    ttl_minutes: float | None,
) -> tuple[list[tuple[str, str]], bool]:
    """(embed fields, is_warning) for /health. `ttl_minutes` is the leak
    forecaster's minutes-to-limit (None = not trending up). Pure — the
    percent-of-limit and warning threshold are unit-tested here, not in Discord."""
    pct = (memory_mb / limit_mb * 100) if limit_mb else 0.0
    fields = [
        ("Memory", f"{memory_mb:,.0f} MB · {pct:.0f}% of the {limit_mb:,} MB watchdog limit"),
        ("CPU", f"{cpu:.0f}%"),
        ("Server FPS", str(fps)),
        ("Frame time", f"{frame_time:.1f} ms"),
    ]
    if ttl_minutes is None:
        fields.append(("Leak forecast", "memory isn't trending up — no restart forecast"))
    else:
        fields.append(
            ("Leak forecast",
             f"on the current pace, memory hits the limit in {leak.fmt_minutes(ttl_minutes)}")
        )
    warning = (ttl_minutes is not None and ttl_minutes <= 90) or pct >= 90
    return fields, warning


def _format_leaderboard(rows: list[tuple[str, float]]) -> str:
    """Medal-ranked playtime list for /leaderboard, from (name, minutes) rows."""
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for i, (name, minutes) in enumerate(rows):
        rank = medals[i] if i < len(medals) else f"**{i + 1}.**"
        lines.append(f"{rank} {name} — {minutes / 60:.1f}h")
    return "\n".join(lines)


def _format_schedule(schedule, now: datetime) -> str:
    """Human overview of upcoming automated actions for /next. `now` is passed
    in so the next-occurrence maths is deterministic under test."""
    from .scheduler import backup_interval_hours, next_daily

    if not schedule.enabled:
        return "⏸️ Scheduling is off — no automatic restarts, backups, or updates."
    lines = []
    if schedule.daily_restart:
        nxt = next_daily(now, schedule.daily_restart_at, 6)
        lines.append(f"🔁 Daily restart at **{schedule.daily_restart_at}** — next {nxt:%a %H:%M}")
    else:
        lines.append("🔁 Daily restart: off")
    hours = backup_interval_hours(schedule.backup_hours)
    if hours <= 0:
        # backup_hours <= 0 is the documented "off" escape hatch, and
        # _backup_loop honours it — don't imply the safety-net backups run.
        lines.append("📦 Backups: off")
    else:
        lines.append(f"📦 Backups every **{hours}h** (keeping {schedule.backup_retain})")
    lines.append(f"💾 Autosave every **{schedule.autosave_minutes}m**")
    if schedule.auto_update:
        nxt = next_daily(now, schedule.auto_update_at, 5)
        lines.append(f"⏬ Auto-update at **{schedule.auto_update_at}** — next {nxt:%a %H:%M}")
    else:
        lines.append("⏬ Auto-update: off")
    return "\n".join(lines)


class _ConfirmView(discord.ui.View):
    """A Confirm/Cancel button pair for destructive commands, so a fat-fingered
    /restore, /stop, or /update from a phone asks first. Only the admin who
    invoked the command may press the buttons, and it auto-expires so a stale
    prompt can't be actioned later."""

    def __init__(self, author_id: int, *, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation isn't yours.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def _confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()


class PalBot(discord.Client):
    def __init__(
        self,
        cfg: Config,
        api: PalApi,
        bus: EventBus,
        store: SessionStore,
        scheduler,  # Scheduler, avoid circular import
    ) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._cfg = cfg
        self._api = api
        self._bus = bus
        self._store = store
        self._sched = scheduler
        self._bg_tasks: set[asyncio.Task] = set()
        self._status_started = False
        # Events queue here; _event_pump does the actual Discord sends so
        # EventBus.emit (the poll/watchdog/scheduler path) never waits on
        # Discord. Bounded: a long Discord outage drops oldest, keeps newest.
        self._events: asyncio.Queue[Event] = asyncio.Queue(maxsize=200)
        self._register()
        bus.on_any(self._on_event)

    def _spawn(self, coro) -> None:
        """Fire-and-forget with a strong ref (asyncio only keeps weak ones) and
        exception logging — a failed /update or /restore must leave a trace in
        the file log, not just asyncio's GC-time stderr warning that service
        mode discards."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._spawned_done)

    def _spawned_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger("palctl.bot").error(
                "bot background task failed", exc_info=task.exception()
            )

    async def _run_reserved(self, name: str, coro) -> None:
        """Run a reserved fire-and-forget operation, clearing the reservation
        when it finishes (or fails before ever taking the real lock). Pairs with
        self._sched.reserve(name) — see the /restart and /update commands."""
        try:
            await coro
        finally:
            self._sched.clear_reservation(name)

    def reconfigure(self, cfg: Config, api: PalApi) -> None:
        """Pick up a config reload: channel, notification toggles, API endpoint.

        Only reaches a *live* bot — when run_bot has already returned (bot was
        disabled, no token, token rejected) the daemon relaunches it on reload
        instead. Changing the token of a running bot still needs a daemon
        restart: discord.py can't swap tokens on a live client.
        """
        self._cfg = cfg
        self._api = api

    # ---------- event relay ----------

    async def _channel(self) -> discord.abc.Messageable | None:
        cid = self._cfg.discord.channel_id
        if not cid:
            return None
        ch = self.get_channel(cid)
        if ch is None:
            # Not in the cache yet (e.g. right after connect) — ask the API.
            try:
                ch = await self.fetch_channel(cid)
            except discord.DiscordException:
                return None
        return ch if isinstance(ch, discord.abc.Messageable) else None

    async def _on_event(self, e: Event) -> None:
        # Never do Discord I/O here: this runs inside EventBus.emit, i.e.
        # inside the poll loop, the watchdog, and every operation that emits
        # while holding the server lock. discord.py sleeps through rate
        # limits inside send(), so one busy channel would stall all of them.
        # Queue instead; _event_pump (a bot task) absorbs the latency.
        try:
            self._events.put_nowait(e)
        except asyncio.QueueFull:
            # Discord has been unreachable a while — drop the oldest, keep
            # the newest; stale join/leave noise is worth less than "down".
            with contextlib.suppress(asyncio.QueueEmpty):
                self._events.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._events.put_nowait(e)

    async def _event_pump(self) -> None:
        while not self.is_closed():
            e = await self._events.get()
            try:
                await self._relay_event(e)
            except Exception:
                logging.getLogger("palctl.bot").warning(
                    "event relay to Discord failed", exc_info=True
                )

    async def _relay_event(self, e: Event) -> None:
        d = self._cfg.discord
        if not d.enabled:
            return

        wants = {
            "join": d.notify_join_leave,
            "leave": d.notify_join_leave,
            "levelup": d.notify_level_up,
            "watchdog": d.notify_watchdog,
            "server_up": d.notify_server_up_down,
            "server_down": d.notify_server_up_down,
            "restart": True,
            "backup": True,
            "update": True,
            "restore": True,
            "update_available": d.notify_update_available,
            "error": True,
        }
        if not wants.get(e.kind, False):
            return

        ch = await self._channel()
        if ch is None:
            return

        colour = (
            RED if e.kind in ("server_down", "error")
            else GREEN if e.kind in ("server_up", "join")
            else BLUE
        )
        try:
            await ch.send(embed=discord.Embed(description=e.message, colour=colour))
            if e.kind == "join" and d.welcome_message:
                name = e.data.get("name", "there")
                # Player-chosen names are untrusted: never let one ping
                # @everyone or roles through the welcome message.
                await ch.send(
                    d.welcome_message.replace("{name}", name),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.DiscordException:
            pass

    # ---------- permissions ----------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        return _admin_allowed(
            self._cfg.discord.admin_role_id,
            getattr(interaction.user, "id", 0),
            role_ids,
            bool(perms and perms.manage_guild),
        )

    # ---------- status embed (shared by /status and the live message) ----------

    async def _status_embed(self) -> discord.Embed:
        # Both are blocking (sc.exe / a full psutil scan) — keep them off the
        # shared event loop.
        svc = await asyncio.to_thread(procs.service_state, self._cfg.service_name)
        stats = await asyncio.to_thread(procs.proc_stats)

        e = discord.Embed(title="Palworld Server", colour=BLUE)
        e.add_field(name="Service", value=svc)
        try:
            m = await self._api.metrics()
            e.add_field(name="Players", value=f"{m.current_players}/{m.max_players}")
            e.add_field(name="Server FPS", value=str(m.server_fps))
            e.add_field(name="Frame time", value=f"{m.server_frame_time:.1f} ms")
            e.add_field(name="Uptime", value=_fmt_uptime(m.uptime))
            e.add_field(name="In-game day", value=str(m.days))
            e.add_field(name="Base camps", value=str(m.base_camps))
        except PalApiError as err:
            e.add_field(name="REST API", value=f"unreachable — {err}", inline=False)
            e.colour = RED

        if stats:
            limit = self._cfg.watchdog.memory_limit_mb
            pct = stats.memory_mb / limit * 100 if limit else 0
            e.add_field(
                name="Memory",
                value=f"{stats.memory_mb:,.0f} MB ({pct:.0f}% of watchdog limit)",
            )
            e.add_field(name="CPU", value=f"{stats.cpu_percent:.0f}%")

        # The leak forecast, when there is one — so the live status embed (and
        # /status) answers "is a restart coming?" at a glance, not just /health.
        samples = [
            (s["at"], s["memory_mb"])
            for s in await asyncio.to_thread(self._store.recent_metrics, 720)
            if s.get("memory_mb")
        ]
        ttl = leak.time_to_limit_minutes(samples, self._cfg.watchdog.memory_limit_mb)
        if ttl is not None:
            e.add_field(
                name="Leak forecast",
                value=f"memory hits the limit in {leak.fmt_minutes(ttl)}",
                inline=False,
            )
        return e

    async def _status_loop(self) -> None:
        """Keep one embed refreshed in place, rather than spamming the channel."""
        msg = None
        while not self.is_closed():
            await asyncio.sleep(60)
            if not (self._cfg.discord.enabled and self._cfg.discord.status_message):
                continue
            ch = await self._channel()
            if ch is None:
                continue
            try:
                embed = await self._status_embed()
                embed.title = "Palworld Server — live"
                msg = await msg.edit(embed=embed) if msg else await ch.send(embed=embed)
            except discord.DiscordException:
                msg = None  # message deleted or channel gone; re-post next tick

    # ---------- commands ----------

    def _register(self) -> None:
        tree = self.tree

        @tree.command(name="status", description="Server status, FPS, memory, uptime")
        async def status(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(embed=await self._status_embed())

        @tree.command(name="players", description="Who's online")
        async def players(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            try:
                ps = await self._api.players()
            except PalApiError as err:
                await interaction.followup.send(f"❌ {err}")
                return

            if not ps:
                await interaction.followup.send("Nobody online.")
                return

            lines = [
                f"**{p.name}** — lvl {p.level} · {p.ping:.0f}ms · "
                f"{p.building_count} buildings"
                for p in ps
            ]
            e = discord.Embed(
                title=f"{len(ps)} online", description="\n".join(lines), colour=BLUE
            )
            await interaction.followup.send(embed=e)

        @tree.command(
            name="playtime", description="Total playtime for a player (online or offline)"
        )
        @app_commands.describe(name="Player name")
        async def playtime(interaction: discord.Interaction, name: str) -> None:
            await self._playtime(interaction, name)

        @playtime.autocomplete("name")
        async def _playtime_ac(interaction: discord.Interaction, current: str):
            return await self._known_name_choices(interaction, current)

        @tree.command(name="announce", description="Send an in-game announcement")
        async def announce(interaction: discord.Interaction, message: str) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            await interaction.response.defer()
            try:
                await self._api.announce(message)
                await interaction.followup.send(f"📣 Announced: {message}")
            except PalApiError as err:
                await interaction.followup.send(f"❌ {err}")

        @tree.command(name="save", description="Save the world now")
        async def save(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            await interaction.response.defer()
            try:
                await self._api.save()
                await interaction.followup.send("💾 World saved.")
            except PalApiError as err:
                await interaction.followup.send(f"❌ {err}")

        @tree.command(name="backup", description="Take a backup now")
        async def backup(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            await interaction.response.defer()
            await self._sched.backup_now("discord")
            await interaction.followup.send("📦 Backup started.")

        @tree.command(name="restart", description="Restart with an in-game countdown")
        @app_commands.describe(reason="Shown to players in-game")
        async def restart(
            interaction: discord.Interaction, reason: str = "Admin restart"
        ) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            # Reserve up front so a second /restart (or a restart while an update
            # is mid-flight) reports 'busy' instead of silently queueing another
            # full countdown behind the first — parity with the HTTP /action path.
            if not self._sched.reserve("restart"):
                await interaction.response.send_message(
                    f"⏳ The server is mid-operation ({self._sched.current_op}) — "
                    "try again in a moment.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"🔁 Restarting with countdown — *{reason}*. I'll report back."
            )
            self._spawn(
                self._run_reserved("restart", self._sched.restart_with_countdown(reason))
            )

        @tree.command(
            name="update", description="Update the server via SteamCMD (stops it first)"
        )
        async def update(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            if not await self._confirm(
                interaction,
                "⏬ **Update the server via SteamCMD?** It goes down for the update; "
                "a pre-update world backup is taken first.",
            ):
                return
            # Reserve only AFTER the confirm dialog (don't hold the server
            # reserved while waiting on a button press), so a busy server is
            # reported rather than queueing a second update behind the first.
            if not self._sched.reserve("update"):
                await interaction.followup.send(
                    f"⏳ The server is mid-operation ({self._sched.current_op}) — "
                    "try again in a moment."
                )
                return
            await interaction.followup.send(
                "⏬ Updating the server via SteamCMD — I'll report back here when "
                "it's finished."
            )
            self._spawn(self._run_reserved("update", self._sched.update_server()))

        @tree.command(name="backups", description="List the most recent backups")
        async def backups_cmd(interaction: discord.Interaction) -> None:
            from pathlib import Path

            await interaction.response.defer()
            bs = await asyncio.to_thread(
                backups.listing, Path(self._cfg.backup_root)
            )
            if not bs:
                await interaction.followup.send("No backups yet.")
                return
            lines = [f"`{b.name}` — {b.size_mb:.0f} MB" for b in bs[:15]]
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Recent backups", description="\n".join(lines), colour=BLUE
                )
            )

        @tree.command(
            name="restore", description="Restore a backup by name (stops the server)"
        )
        @app_commands.describe(name="Backup name, as shown by /backups")
        async def restore(interaction: discord.Interaction, name: str) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            if not await self._confirm(
                interaction,
                f"♻️ **Restore `{name}`?** The server stops and restarts. A safety "
                "copy of the current world is taken first, so this is undoable.",
            ):
                return
            await interaction.followup.send(
                f"♻️ Restoring `{name}` — I'll report back here."
            )
            self._spawn(self._sched.restore_backup(name))

        @restore.autocomplete("name")
        async def _restore_ac(interaction: discord.Interaction, current: str):
            return await self._backup_name_choices(interaction, current)

        @tree.command(name="kick", description="Kick a player")
        async def kick(
            interaction: discord.Interaction, name: str, reason: str = "Kicked by admin"
        ) -> None:
            await self._moderate(interaction, name, reason, "kick")

        @kick.autocomplete("name")
        async def _kick_ac(interaction: discord.Interaction, current: str):
            return await self._player_name_choices(interaction, current)

        @tree.command(name="ban", description="Ban a player")
        async def ban(
            interaction: discord.Interaction, name: str, reason: str = "Banned by admin"
        ) -> None:
            await self._moderate(interaction, name, reason, "ban")

        @ban.autocomplete("name")
        async def _ban_ac(interaction: discord.Interaction, current: str):
            return await self._player_name_choices(interaction, current)

        @tree.command(name="unban", description="Unban a player by user ID")
        async def unban(interaction: discord.Interaction, user_id: str) -> None:
            # Banned players are offline, so the user ID (from the ban
            # message or /players) is the only handle — no name resolution.
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            await interaction.response.defer()
            try:
                await self._api.unban(user_id)
                await interaction.followup.send(f"✅ Unbanned `{user_id}`.")
            except PalApiError as err:
                await interaction.followup.send(f"❌ {err}")

        @tree.command(name="start", description="Start the server")
        async def start(interaction: discord.Interaction) -> None:
            await self._server_power(interaction, "start")

        @tree.command(name="stop", description="Save and stop the server (asks first)")
        async def stop(interaction: discord.Interaction) -> None:
            await self._server_power(interaction, "stop")

        @tree.command(name="health", description="Memory, CPU, FPS, and leak forecast")
        async def health(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(embed=await self._health_embed())

        @tree.command(name="leaderboard", description="Top players by total playtime")
        @app_commands.describe(top="How many to show (1–25, default 10)")
        async def leaderboard(interaction: discord.Interaction, top: int = 10) -> None:
            await interaction.response.defer()
            rows = await asyncio.to_thread(self._store.top_playtime, max(1, min(25, top)))
            if not rows:
                await interaction.followup.send(
                    "No playtime recorded yet — it accrues as people play (only "
                    "sessions that have ended count)."
                )
                return
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🏆 Playtime leaderboard",
                    description=_format_leaderboard(rows),
                    colour=BLUE,
                )
            )

        @tree.command(name="whois", description="Details for a player (online or offline)")
        @app_commands.describe(name="Player name")
        async def whois(interaction: discord.Interaction, name: str) -> None:
            await self._whois(interaction, name)

        @whois.autocomplete("name")
        async def _whois_ac(interaction: discord.Interaction, current: str):
            return await self._known_name_choices(interaction, current)

        @tree.command(name="events", description="Recent server events")
        @app_commands.describe(count="How many to show (1–25, default 12)")
        async def events(interaction: discord.Interaction, count: int = 12) -> None:
            await interaction.response.defer()
            # Filter first (non-admins don't see error/watchdog internals), then
            # take the most recent `count` of what's left.
            evs = _visible_events(self._bus.recent(60), self._is_admin(interaction))
            evs = evs[-max(1, min(25, count)):]
            if not evs:
                await interaction.followup.send("No events recorded yet.")
                return
            lines = [f"`{e.at:%m-%d %H:%M}` {e.message}" for e in reversed(evs)]
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Recent events", description="\n".join(lines), colour=BLUE
                )
            )

        @tree.command(name="cancel", description="Cancel an in-progress restart countdown")
        async def cancel(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            await interaction.response.defer()
            if self._sched.cancel_countdown():
                await interaction.followup.send(
                    "🚫 Cancelling the restart countdown — the server stays up."
                )
            else:
                await interaction.followup.send("Nothing to cancel — no countdown is running.")

        @tree.command(
            name="next", description="Upcoming automatic restarts, backups, and updates"
        )
        async def next_(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            text = _format_schedule(self._cfg.schedule, datetime.now())
            await interaction.followup.send(
                embed=discord.Embed(title="Scheduled tasks", description=text, colour=BLUE)
            )

        @tree.command(name="help", description="What palctl's bot can do")
        async def help_(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=self._help_embed(), ephemeral=True)

    def _help_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="palctl bot",
            description="Self-hosted Palworld server control — reconstructed from "
            "the REST API, no cloud bridge.",
            colour=BLUE,
        )
        e.add_field(
            name="Anyone",
            value="/status · /health · /players · /whois · /playtime · /leaderboard "
            "· /backups · /events · /next · /help",
            inline=False,
        )
        e.add_field(
            name="Admins",
            value="/start · /stop · /restart · /cancel · /update · /save · /backup "
            "· /restore · /announce · /kick · /ban · /unban",
            inline=False,
        )
        e.set_footer(
            text="Admin commands need the configured role/user (or Manage Server if "
            "none is set)."
        )
        return e

    async def _moderate(
        self, interaction: discord.Interaction, name: str, reason: str, action: str
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        await interaction.response.defer()

        try:
            ps = await self._api.players()
            # An exact user ID wins outright — it's the only unambiguous
            # handle when two players share a name.
            match = next((p for p in ps if p.user_id == name), None)
            if match is None:
                matches = [p for p in ps if p.name.lower() == name.lower()]
                if not matches:
                    await interaction.followup.send(f"**{name}** isn't online.")
                    return
                if len(matches) > 1:
                    ids = ", ".join(f"`{p.user_id}`" for p in matches)
                    await interaction.followup.send(
                        f"{len(matches)} online players are named **{name}** "
                        f"({ids}) — re-run /{action} with the exact user ID so "
                        "the right one is hit."
                    )
                    return
                match = matches[0]

            if action == "kick":
                await self._api.kick(match.user_id, reason)
            else:
                await self._api.ban(match.user_id, reason)

            await interaction.followup.send(f"✅ {action.title()}ed **{match.name}** — {reason}")
        except PalApiError as err:
            await interaction.followup.send(f"❌ {err}")

    # ---------- confirmation ----------

    async def _confirm(self, interaction: discord.Interaction, prompt: str) -> bool:
        """Post a Confirm/Cancel prompt to the invoking admin and wait. Returns
        True only if they pressed Confirm before it timed out. Consumes the
        interaction's initial response, so callers use followup afterwards."""
        view = _ConfirmView(interaction.user.id)
        await interaction.response.send_message(prompt, view=view)
        await view.wait()
        outcome = "✅ Confirmed." if view.value else "✖️ Cancelled."
        with contextlib.suppress(discord.DiscordException):
            await interaction.edit_original_response(content=f"{prompt}\n{outcome}", view=None)
        return view.value is True

    # ---------- start / stop ----------

    async def _server_power(self, interaction: discord.Interaction, action: str) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        if action == "stop":
            if not await self._confirm(
                interaction,
                "⏹️ **Stop the server?** Everyone online is disconnected, and it "
                "stays down until someone starts it again.",
            ):
                return
            result = await self._sched.stop_server()
            await interaction.followup.send(
                {
                    "ok": "⏹️ World saved and server stopped.",
                    "busy": "⏳ The server is mid-operation — try again in a moment.",
                    "failed": "⚠️ The stop didn't confirm — the server may be hung. "
                    "Check the box, or try /restart.",
                }[result]
            )
            return
        # start
        await interaction.response.defer()
        result = await self._sched.start_server()
        await interaction.followup.send(
            {
                "ok": "▶️ Starting the server — I'll post here when it's up "
                "(the REST API takes about a minute to answer).",
                "busy": "⏳ The server is mid-operation — try again in a moment.",
            }[result]
        )

    # ---------- richer reads ----------

    async def _whois(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        try:
            ps = await self._api.players()
        except PalApiError:
            ps = []  # server down / unreachable — still answer from history below
        match = next(
            (p for p in ps if p.name.lower() == name.lower() or p.user_id == name), None
        )
        admin = self._is_admin(interaction)

        if match is not None:
            mins = await asyncio.to_thread(
                self._store.total_playtime_minutes, match.user_id
            )
            e = discord.Embed(title=f"{match.name} — online", colour=GREEN)
            e.add_field(name="Level", value=str(match.level))
            e.add_field(name="Ping", value=f"{match.ping:.0f} ms")
            e.add_field(name="Buildings", value=str(match.building_count))
            e.add_field(name="Playtime", value=f"{mins / 60:.1f}h")
            await interaction.followup.send(embed=e)
            # A player's live map position (a grief vector) and platform ID (the
            # dox / ban handle) go ONLY to the admin who asked — a public embed
            # would broadcast them to everyone in the channel.
            if admin:
                priv = discord.Embed(title=f"{match.name} — admin details", colour=BLUE)
                priv.add_field(
                    name="Location",
                    value=f"{match.location_x:.0f}, {match.location_y:.0f}",
                )
                priv.add_field(name="User ID", value=f"`{match.user_id}`")
                await interaction.followup.send(embed=priv, ephemeral=True)
            return

        # Offline: fall back to what the session history remembers.
        uid = await asyncio.to_thread(self._store.resolve_user_id, name)
        if uid is None:
            await interaction.followup.send(
                f"I've never seen a player called **{name}** — names come from who's "
                "played while palctl was running."
            )
            return
        seen = await asyncio.to_thread(self._store.last_seen, uid)
        mins = await asyncio.to_thread(self._store.total_playtime_minutes, uid)
        display = seen[0] if seen else name
        e = discord.Embed(title=f"{display} — offline", colour=BLUE)
        e.add_field(name="Playtime", value=f"{mins / 60:.1f}h")
        if seen:
            e.add_field(name="Last seen", value=_fmt_last_seen(seen[1]))
        await interaction.followup.send(embed=e)
        if admin:  # platform ID only to the admin who asked
            await interaction.followup.send(
                f"🔑 `{display}` user ID: `{uid}`", ephemeral=True
            )

    async def _playtime(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        try:
            online = await self._api.players()
        except PalApiError:
            online = []
        match = next(
            (p for p in online if p.name.lower() == name.lower() or p.user_id == name),
            None,
        )
        if match is not None:
            uid, display, suffix = match.user_id, match.name, f", currently level {match.level}"
        else:
            uid = await asyncio.to_thread(self._store.resolve_user_id, name)
            if uid is None:
                await interaction.followup.send(
                    f"I've never seen a player called **{name}** — names come from "
                    "who's played while palctl was running."
                )
                return
            seen = await asyncio.to_thread(self._store.last_seen, uid)
            display = seen[0] if seen else name
            suffix = " (offline)"
        mins = await asyncio.to_thread(self._store.total_playtime_minutes, uid)
        await interaction.followup.send(f"**{display}** — {mins / 60:.1f}h total{suffix}.")

    async def _health_embed(self) -> discord.Embed:
        stats = await asyncio.to_thread(procs.proc_stats)
        wd = self._cfg.watchdog
        fps = frame_time = 0
        try:
            m = await self._api.metrics()
            fps, frame_time = m.server_fps, m.server_frame_time
        except PalApiError:
            pass
        # Forecast off the persisted history (survives daemon restarts), same
        # data the daemon's own predictor fits.
        samples = [
            (s["at"], s["memory_mb"])
            for s in await asyncio.to_thread(self._store.recent_metrics, 720)
            if s.get("memory_mb")
        ]
        ttl = leak.time_to_limit_minutes(samples, wd.memory_limit_mb)
        fields, warning = _health_fields(
            memory_mb=stats.memory_mb if stats else 0.0,
            limit_mb=wd.memory_limit_mb,
            cpu=stats.cpu_percent if stats else 0.0,
            fps=fps,
            frame_time=frame_time,
            ttl_minutes=ttl,
        )
        e = discord.Embed(title="Server health", colour=RED if warning else GREEN)
        for name, value in fields:
            e.add_field(name=name, value=value, inline=False)
        if not stats:
            e.description = "The Palworld server process isn't running right now."
        return e

    # ---------- autocomplete sources ----------

    async def _player_name_choices(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            ps = await self._api.players()
        except PalApiError:
            return []
        names = _match_choices([p.name for p in ps], current)
        return [app_commands.Choice(name=n, value=n) for n in names]

    async def _known_name_choices(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Online players first, then names from session history — the pool for
        /playtime and /whois, which answer for offline players too."""
        try:
            online = [p.name for p in await self._api.players()]
        except PalApiError:
            online = []
        recent = await asyncio.to_thread(self._store.recent_player_names, 50)
        seen: set[str] = set()
        merged: list[str] = []
        for n in (*online, *recent):
            if n.lower() in seen:
                continue
            seen.add(n.lower())
            merged.append(n)
        names = _match_choices(merged, current)
        return [app_commands.Choice(name=n, value=n) for n in names]

    async def _backup_name_choices(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        from pathlib import Path

        try:
            bs = await asyncio.to_thread(backups.listing, Path(self._cfg.backup_root))
        except Exception:
            return []
        names = _match_choices([b.name for b in bs], current)
        return [app_commands.Choice(name=n, value=n) for n in names]

    # ---------- lifecycle ----------

    async def setup_hook(self) -> None:
        self._spawn(self._event_pump())
        await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"[discord] connected as {self.user}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the Palworld server"
            )
        )
        # on_ready can fire again on reconnect; only ever start one status loop.
        if not self._status_started:
            self._status_started = True
            self._spawn(self._status_loop())


async def run_bot(
    cfg: Config,
    api: PalApi,
    bus: EventBus,
    store: SessionStore,
    sched,
    on_created=None,
):
    token = get_discord_token()
    if not (cfg.discord.enabled and token):
        return  # bot is opt-in; daemon runs fine without it

    # discord.py only auto-reconnects AFTER the first successful login; a
    # network that isn't up yet at daemon start (boot order, DNS not ready)
    # used to kill the bot until the daemon was restarted. Retry the initial
    # connect with a backoff. A closed Client can't be started again, so each
    # attempt rebuilds it (and unhooks the dead one from the bus).
    attempt = 0
    while True:
        bot = PalBot(cfg, api, bus, store, sched)
        if on_created is not None:
            # Hand the instance back to the daemon so reload-config can reach it.
            on_created(bot)
        try:
            await bot.start(token)
            return  # clean shutdown
        except discord.LoginFailure:
            await bus.emit(
                Event("error", "Discord token rejected. Re-enter it in palctl Settings.")
            )
            return  # retrying a bad token just fails again
        except Exception as e:
            # The bot is optional; it must never take the watchdog and scheduler
            # down with it (they all run in the same asyncio.gather).
            if bot._status_started:
                # It WAS connected — discord.py's own reconnect gave up.
                await bus.emit(Event("error", f"Discord bot stopped: {e}"))
                return
            attempt += 1
            delay = min(300, 15 * 2 ** min(attempt - 1, 4))
            await bus.emit(
                Event(
                    "error",
                    f"Discord bot couldn't connect ({e}) — retrying in {delay}s.",
                )
            )
        finally:
            bus.off_any(bot._on_event)
            if not bot.is_closed():
                with contextlib.suppress(Exception):
                    await bot.close()
        await asyncio.sleep(delay)
