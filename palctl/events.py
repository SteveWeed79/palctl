"""
Event bus and derived state.

Palworld gives us no event stream — no chat, no join/leave hooks, no log file.
What it gives us is a player list we can poll. So we poll and *diff*, and
synthesise the events ourselves:

    join / leave      <- player appears or disappears from /players
    level up          <- the `level` field changed between two polls
    base sprawl       <- `building_count` changed
    server up / down  <- /metrics started or stopped answering

That's most of what a Minecraft-style bot gives you, reconstructed from the only
surface Pocketpair exposes. What we cannot fake is chat — there is no chat-read
endpoint and dedicated servers ship no log. That gap is UE4SS-only.

Session tracking (playtime, last seen) falls out of join/leave for free. Takaro
charges for that.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api import Player
from .config import config_dir

# ---------------- events ----------------


@dataclass(frozen=True)
class Event:
    kind: str  # join | leave | levelup | server_up | server_down | watchdog | backup | error
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Dead simple pub/sub. The Discord bot and the GUI both just subscribe."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._recent: list[Event] = []

    def on(self, kind: str, handler: Handler) -> None:
        self._handlers[kind].append(handler)

    def on_any(self, handler: Handler) -> None:
        self._handlers["*"].append(handler)

    async def emit(self, event: Event) -> None:
        self._recent.append(event)
        del self._recent[:-500]

        for h in [*self._handlers.get(event.kind, []), *self._handlers.get("*", [])]:
            try:
                await h(event)
            except Exception as e:  # a broken subscriber must not kill the daemon
                print(f"[eventbus] handler failed for {event.kind}: {e}")

    def recent(self, n: int = 50) -> list[Event]:
        return self._recent[-n:]


# ---------------- session store ----------------

DB_PATH = config_dir() / "sessions.db"


class SessionStore:
    """Playtime and history. Palworld remembers none of this; we do."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        # log_event runs on worker threads (asyncio.to_thread) while the loop
        # thread reads/writes sessions; one lock serialises all access.
        self._lock = threading.Lock()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                user_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                joined_at   TEXT NOT NULL,
                left_at     TEXT,
                level_start INTEGER,
                level_end   INTEGER
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                at      TEXT NOT NULL,
                kind    TEXT NOT NULL,
                message TEXT NOT NULL,
                data    TEXT
            )
            """
        )
        # Sessions a previous daemon run never closed can't be closed correctly
        # (we don't know when the player left), and they'd shadow the player's
        # next real session in close_session(). Close them at zero length.
        self._db.execute("UPDATE sessions SET left_at = joined_at WHERE left_at IS NULL")
        self._db.commit()

    def open_session(self, p: Player) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (user_id, name, joined_at, level_start) VALUES (?,?,?,?)",
                (p.user_id, p.name, datetime.now(UTC).isoformat(), p.level),
            )
            self._db.commit()

    def close_session(self, user_id: str, level: int) -> float:
        """Returns session length in minutes."""
        with self._lock:
            row = self._db.execute(
                "SELECT rowid, joined_at FROM sessions "
                "WHERE user_id=? AND left_at IS NULL ORDER BY rowid DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                return 0.0

            rowid, joined_at = row
            now = datetime.now(UTC)
            self._db.execute(
                "UPDATE sessions SET left_at=?, level_end=? WHERE rowid=?",
                (now.isoformat(), level, rowid),
            )
            self._db.commit()
            return (now - datetime.fromisoformat(joined_at)).total_seconds() / 60

    def total_playtime_minutes(self, user_id: str) -> float:
        with self._lock:
            rows = self._db.execute(
                "SELECT joined_at, left_at FROM sessions WHERE user_id=? AND left_at IS NOT NULL",
                (user_id,),
            ).fetchall()
        total = 0.0
        for joined, left in rows:
            total += (
                datetime.fromisoformat(left) - datetime.fromisoformat(joined)
            ).total_seconds() / 60
        return total

    def log_event(self, e: Event) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (at, kind, message, data) VALUES (?,?,?,?)",
                (e.at.isoformat(), e.kind, e.message, json.dumps(e.data)),
            )
            self._db.commit()


# ---------------- the differ ----------------


class PlayerTracker:
    """Turns successive /players snapshots into join/leave/levelup events."""

    def __init__(self, bus: EventBus, store: SessionStore) -> None:
        self._bus = bus
        self._store = store
        self._known: dict[str, Player] = {}
        self._primed = False

    async def update(self, players: list[Player]) -> None:
        current = {p.user_id: p for p in players if p.user_id}

        # First poll after startup: adopt state silently. Otherwise a daemon
        # restart spams "5 players joined!" for people who were already on.
        if not self._primed:
            self._known = current
            self._primed = True
            for p in current.values():
                self._store.open_session(p)
            return

        for uid, p in current.items():
            if uid not in self._known:
                self._store.open_session(p)
                await self._bus.emit(
                    Event("join", f"**{p.name}** joined (level {p.level})",
                          {"name": p.name, "user_id": uid, "level": p.level})
                )
            else:
                old = self._known[uid]
                if p.level > old.level:
                    await self._bus.emit(
                        Event("levelup", f"**{p.name}** reached level **{p.level}**",
                              {"name": p.name, "user_id": uid,
                               "from": old.level, "to": p.level})
                    )

        for uid, old in self._known.items():
            if uid not in current:
                minutes = self._store.close_session(uid, old.level)
                await self._bus.emit(
                    Event("leave",
                          f"**{old.name}** left (played {minutes:.0f}m)",
                          {"name": old.name, "user_id": uid, "minutes": minutes})
                )

        self._known = current

    async def handle_server_down(self) -> None:
        """Server died. Close sessions rather than leaving them dangling forever."""
        for uid, p in self._known.items():
            self._store.close_session(uid, p.level)
        self._known = {}
        self._primed = False

    @property
    def online(self) -> list[Player]:
        return list(self._known.values())
