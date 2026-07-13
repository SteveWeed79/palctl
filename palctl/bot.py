"""
Discord bot.

Self-hosted, your own token, your own box. No cloud bridge, no subscription.

Read this before you compare it to your Hytale bot: there is NO chat relay, and
there can't be. Palworld exposes no chat-read endpoint and dedicated servers
ship no log file. Chat is UE4SS-only territory.

What you DO get, all reconstructed from polling /players and /metrics:
  /status /players /announce /save /restart /backup /kick /ban /playtime
  join + leave alerts, level-up alerts, watchdog alerts, up/down alerts
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import discord
from discord import app_commands

from . import backups, procs
from .api import PalApi, PalApiError
from .config import Config, get_discord_token
from .events import Event, EventBus, SessionStore

BLUE = 0x47C8FF
RED = 0xF85149
GREEN = 0x3FB950


def _fmt_uptime(seconds: float) -> str:
    d = timedelta(seconds=int(seconds))
    h, rem = divmod(d.seconds, 3600)
    m = rem // 60
    return f"{d.days}d {h}h {m}m" if d.days else f"{h}h {m}m"


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
        self._register()
        bus.on_any(self._on_event)

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
                await ch.send(d.welcome_message.replace("{name}", name))
        except discord.DiscordException:
            pass

    # ---------- permissions ----------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        role_id = self._cfg.discord.admin_role_id
        if not role_id:
            # No role configured -> only people who can manage the guild.
            perms = getattr(interaction.user, "guild_permissions", None)
            return bool(perms and perms.manage_guild)
        roles = getattr(interaction.user, "roles", [])
        return any(r.id == role_id for r in roles)

    # ---------- status embed (shared by /status and the live message) ----------

    async def _status_embed(self) -> discord.Embed:
        svc = procs.service_state(self._cfg.service_name)
        stats = procs.proc_stats()

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

        @tree.command(name="playtime", description="Total playtime for a player")
        @app_commands.describe(name="Player name (as shown in /players)")
        async def playtime(interaction: discord.Interaction, name: str) -> None:
            await interaction.response.defer()
            try:
                ps = await self._api.players()
            except PalApiError:
                ps = []

            match = next((p for p in ps if p.name.lower() == name.lower()), None)
            if match is None:
                await interaction.followup.send(
                    f"Can't find **{name}** online. (Playtime lookup needs them "
                    "on the server so I can resolve their user ID.)"
                )
                return

            mins = self._store.total_playtime_minutes(match.user_id)
            await interaction.followup.send(
                f"**{match.name}** — {mins / 60:.1f}h total, currently level {match.level}."
            )

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
            await interaction.response.send_message(
                f"🔁 Restarting with countdown — *{reason}*. I'll report back."
            )
            # asyncio holds only weak refs to tasks; keep one for the countdown.
            task = asyncio.create_task(self._sched.restart_with_countdown(reason))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        @tree.command(
            name="update", description="Update the server via SteamCMD (stops it first)"
        )
        async def update(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Not allowed.", ephemeral=True)
                return
            await interaction.response.send_message(
                "⏬ Updating the server via SteamCMD — it'll go down and I'll report "
                "back here when it's finished."
            )
            task = asyncio.create_task(self._sched.update_server())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

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
            await interaction.response.send_message(
                f"♻️ Restoring `{name}` — the server will restart. A safety copy of "
                "the current world is taken first. I'll report back here."
            )
            task = asyncio.create_task(self._sched.restore_backup(name))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

        @tree.command(name="kick", description="Kick a player")
        async def kick(
            interaction: discord.Interaction, name: str, reason: str = "Kicked by admin"
        ) -> None:
            await self._moderate(interaction, name, reason, "kick")

        @tree.command(name="ban", description="Ban a player")
        async def ban(
            interaction: discord.Interaction, name: str, reason: str = "Banned by admin"
        ) -> None:
            await self._moderate(interaction, name, reason, "ban")

    async def _moderate(
        self, interaction: discord.Interaction, name: str, reason: str, action: str
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
        await interaction.response.defer()

        try:
            ps = await self._api.players()
            match = next((p for p in ps if p.name.lower() == name.lower()), None)
            if match is None:
                await interaction.followup.send(f"**{name}** isn't online.")
                return

            if action == "kick":
                await self._api.kick(match.user_id, reason)
            else:
                await self._api.ban(match.user_id, reason)

            await interaction.followup.send(f"✅ {action.title()}ed **{match.name}** — {reason}")
        except PalApiError as err:
            await interaction.followup.send(f"❌ {err}")

    # ---------- lifecycle ----------

    async def setup_hook(self) -> None:
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
            task = asyncio.create_task(self._status_loop())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)


async def run_bot(cfg: Config, api: PalApi, bus: EventBus, store: SessionStore, sched):
    token = get_discord_token()
    if not (cfg.discord.enabled and token):
        return  # bot is opt-in; daemon runs fine without it

    bot = PalBot(cfg, api, bus, store, sched)
    try:
        await bot.start(token)
    except discord.LoginFailure:
        await bus.emit(
            Event("error", "Discord token rejected. Re-enter it in palctl Settings.")
        )
    except Exception as e:
        # The bot is optional; it must never take the watchdog and scheduler
        # down with it (they all run in the same asyncio.gather).
        await bus.emit(Event("error", f"Discord bot stopped: {e}"))
