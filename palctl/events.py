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
import logging
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

    def off_any(self, handler: Handler) -> None:
        """Detach a catch-all handler. Lets a reconnecting subscriber (the
        Discord bot rebuilds its client per connect attempt) replace itself
        instead of stacking dead handlers forever."""
        try:
            self._handlers["*"].remove(handler)
        except ValueError:
            pass

    async def emit(self, event: Event) -> None:
        self._recent.append(event)
        del self._recent[:-500]

        for h in [*self._handlers.get(event.kind, []), *self._handlers.get("*", [])]:
            try:
                await h(event)
            except Exception:  # a broken subscriber must not kill the daemon
                # The file log, not print(): under the Windows service stdout
                # goes nowhere, and a handler that fails every event (e.g. the
                # Discord bot mis-configured) would be completely invisible.
                logging.getLogger("palctl.events").exception(
                    "event handler failed for %s", event.kind
                )

    def recent(self, n: int = 50) -> list[Event]:
        return self._recent[-n:]


# ---------------- session store ----------------

DB_PATH = config_dir() / "sessions.db"


def _migrate(db: sqlite3.Connection) -> None:
    db.execute(
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
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            at      TEXT NOT NULL,
            kind    TEXT NOT NULL,
            message TEXT NOT NULL,
            data    TEXT
        )
        """
    )
    # One row per poll. This is what lets GUI graphs survive a daemon
    # restart, and what the leak forecaster fits its line to.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS metrics (
            at         REAL NOT NULL,
            fps        INTEGER,
            frame_time REAL,
            players    INTEGER,
            memory_mb  REAL,
            cpu        REAL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_at ON metrics(at)")
    # Sessions a previous daemon run never closed can't be closed correctly
    # (we don't know when the player left), and they'd shadow the player's
    # next real session in close_session(). Close them at zero length.
    db.execute("UPDATE sessions SET left_at = joined_at WHERE left_at IS NULL")
    db.commit()


def _connect_and_migrate(target: Path | str) -> sqlite3.Connection:
    # sqlite3.connect(Path) is fine, but a corrupt file only surfaces at the
    # first statement — running the migration here makes failures happen where
    # _open_store can catch and quarantine them.
    db = sqlite3.connect(target, check_same_thread=False)
    try:
        _migrate(db)
    except BaseException:
        db.close()
        raise
    return db


def _open_store(path: Path) -> sqlite3.Connection:
    """
    A corrupt or unwritable sessions.db must not crash-loop the daemon under
    NSSM — the same policy Config.load applies to config.json: quarantine the
    file and start fresh (history is expendable, the daemon is not), falling
    back to an in-memory store as a last resort.
    """
    log = logging.getLogger("palctl.events")
    try:
        return _connect_and_migrate(path)
    except sqlite3.Error:
        log.exception("sessions.db is unusable — setting it aside as .broken")
    try:
        Path(path).replace(Path(path).with_name(Path(path).name + ".broken"))
        return _connect_and_migrate(path)
    except (sqlite3.Error, OSError):
        log.exception(
            "could not recreate sessions.db — playtime/metrics history will "
            "not persist this run"
        )
        return _connect_and_migrate(":memory:")


class SessionStore:
    """Playtime and history. Palworld remembers none of this; we do."""

    def __init__(self, path: Path = DB_PATH) -> None:
        # log_event runs on worker threads (asyncio.to_thread) while the loop
        # thread reads/writes sessions; one lock serialises all access.
        self._lock = threading.Lock()
        self._db = _open_store(path)

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

    def total_playtime_minutes(self, user_id: str, *, now: datetime | None = None) -> float:
        """Total minutes played, INCLUDING the current session if the player is
        online — an open row (left_at NULL) is counted up to `now`, so /playtime
        reflects the session in progress instead of freezing at the last logout."""
        now = now or datetime.now(UTC)
        with self._lock:
            rows = self._db.execute(
                "SELECT joined_at, left_at FROM sessions WHERE user_id=?",
                (user_id,),
            ).fetchall()
        total = 0.0
        for joined, left in rows:
            try:
                start = datetime.fromisoformat(joined)
                end = datetime.fromisoformat(left) if left else now
            except (ValueError, TypeError):
                continue  # a half-written row shouldn't zero out a real total
            total += (end - start).total_seconds() / 60
        return total

    def resolve_user_id(self, name: str) -> str | None:
        """The most-recent user_id seen for a display name, from session history.
        Lets /playtime and /whois answer for a player who's offline right now —
        the live /players list only knows who's on. Case-insensitive (ASCII)."""
        with self._lock:
            row = self._db.execute(
                "SELECT user_id FROM sessions WHERE name = ? COLLATE NOCASE "
                "ORDER BY rowid DESC LIMIT 1",
                (name,),
            ).fetchone()
        return row[0] if row else None

    def last_seen(self, user_id: str) -> tuple[str, str] | None:
        """(name, left_at-ISO) of the player's most recent finished session, or
        None if they have no closed session on record."""
        with self._lock:
            row = self._db.execute(
                "SELECT name, left_at FROM sessions "
                "WHERE user_id = ? AND left_at IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def recent_player_names(self, limit: int = 50) -> list[str]:
        """Distinct display names from recent sessions, most-recent first. The
        autocomplete pool for /playtime and /whois, which now answer offline."""
        with self._lock:
            rows = self._db.execute(
                "SELECT name FROM sessions ORDER BY rowid DESC"
            ).fetchall()
        seen: set[str] = set()
        out: list[str] = []
        for (name,) in rows:
            low = name.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(name)
            if len(out) >= limit:
                break
        return out

    def top_playtime(
        self, limit: int = 10, *, now: datetime | None = None
    ) -> list[tuple[str, float]]:
        """The `limit` players with the most total playtime, as (name, minutes)
        highest first. Includes the current session for anyone online (an open
        row counted up to `now`). The name is the most recent one seen for that
        account, so a rename shows the current handle. Powers /leaderboard."""
        now = now or datetime.now(UTC)
        with self._lock:
            rows = self._db.execute(
                "SELECT user_id, name, joined_at, left_at FROM sessions ORDER BY rowid"
            ).fetchall()
        totals: dict[str, float] = {}
        latest_name: dict[str, str] = {}
        for user_id, name, joined, left in rows:
            try:
                start = datetime.fromisoformat(joined)
                end = datetime.fromisoformat(left) if left else now
                mins = (end - start).total_seconds() / 60
            except (ValueError, TypeError):
                continue  # a malformed timestamp shouldn't sink the leaderboard
            totals[user_id] = totals.get(user_id, 0.0) + mins
            latest_name[user_id] = name  # rows are rowid-ordered, so last wins
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return [(latest_name[uid], mins) for uid, mins in ranked[: max(0, limit)]]

    METRICS_RETAIN_SECONDS = 7 * 24 * 3600

    def log_metrics(self, s: dict) -> None:
        """Persist one poll sample (a dict with at/fps/frame_time/players/
        memory_mb/cpu). Prunes anything past retention on the way in — at one
        row per poll the delete is cheap and the table stays ~60k rows."""
        with self._lock:
            self._db.execute(
                "INSERT INTO metrics (at, fps, frame_time, players, memory_mb, cpu) "
                "VALUES (?,?,?,?,?,?)",
                (
                    s.get("at", 0.0),
                    s.get("fps", 0),
                    s.get("frame_time", 0.0),
                    s.get("players", 0),
                    s.get("memory_mb", 0.0),
                    s.get("cpu", 0.0),
                ),
            )
            self._db.execute(
                "DELETE FROM metrics WHERE at < ?",
                (s.get("at", 0.0) - self.METRICS_RETAIN_SECONDS,),
            )
            self._db.commit()

    def recent_metrics(self, n: int = 720) -> list[dict]:
        """The newest `n` samples, oldest first — the shape the daemon keeps
        in memory and serves to the GUI."""
        with self._lock:
            rows = self._db.execute(
                "SELECT at, fps, frame_time, players, memory_mb, cpu "
                "FROM metrics ORDER BY at DESC LIMIT ?",
                (n,),
            ).fetchall()
        keys = ("at", "fps", "frame_time", "players", "memory_mb", "cpu")
        return [dict(zip(keys, r, strict=True)) for r in reversed(rows)]

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
