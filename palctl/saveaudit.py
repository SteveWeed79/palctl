"""
What is actually in your world folder, and how much of it is nobody's.

Palworld's `Level.sav` grows without bound. Every player who ever joined leaves
a character record, every guild they formed leaves a group record, and every
base camp they abandoned leaves its own — and none of it is ever collected.
Past a few gigabytes the server spends minutes saving, restarts take longer
than the players will wait, and eventually the world stops loading. It is the
Palworld dedicated-server failure mode, and palctl's memory watchdog treats
only its symptom.

This module is the *diagnosis* half, and deliberately only that. It reads sizes
and filenames — it does not parse, decompress, or modify a single save. Pruning
a `Level.sav` means rewriting the file the server exists to protect, and that
belongs behind a verified backup, an operation lock, and an out-of-process
parser. None of that should be built on a guess about what is in there, so this
measures first.

What makes palctl able to do this well is `sessions.db`: it knows who has
actually played and when. Every other tool in this space has to ask the
operator to guess which players are inactive.

Two honesty rules run through this file, because getting them wrong deletes
somebody's world:

  1. **An unknown player is never an idle one.** A save file palctl cannot
     match to a session is reported as *unknown*, never as inactive. palctl
     only started recording player GUIDs recently, and a world restored from a
     backup, or carried over from before palctl, is full of players it has
     never seen. "I don't know" is a different answer from "nobody wants this".
  2. **The reclaimable figure is a floor, not an estimate.** It counts only the
     per-player `.sav` files, which are small. The real weight is those same
     players' records *inside* `Level.sav`, and measuring that needs a parser
     this module refuses to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# A world folder is SaveGames/<host-id>/<world-id>/, holding Level.sav,
# LevelMeta.sav, WorldOption.sav and a Players/ directory of <guid>.sav.
LEVEL_SAV = "Level.sav"
PLAYERS_DIR = "Players"

# Past this, saves take long enough to be felt as a stall on every autosave and
# a long wait on every restart. Not a cliff — worlds work above it — so it is
# phrased as "worth looking at", never as an error.
LARGE_LEVEL_MB = 1024.0

# How long since a player's last session before their leftovers are *candidates*
# for cleanup. Deliberately long: a season of Palworld is months, and somebody
# coming back to find their pals gone is unrecoverable.
INACTIVE_DAYS = 90


@dataclass(frozen=True)
class PlayerSave:
    """One `Players/<guid>.sav`, and what palctl knows about whose it is."""

    player_id: str
    path: Path
    size_bytes: int
    name: str = ""
    last_seen: datetime | None = None

    @property
    def known(self) -> bool:
        return self.last_seen is not None

    def inactive_for_days(self, now: datetime) -> float | None:
        if self.last_seen is None:
            return None
        return (now - self.last_seen).total_seconds() / 86400


@dataclass(frozen=True)
class SaveAudit:
    world: Path
    level_bytes: int = 0
    total_bytes: int = 0
    players: list[PlayerSave] = field(default_factory=list)

    @property
    def level_mb(self) -> float:
        return self.level_bytes / 1_048_576

    @property
    def total_mb(self) -> float:
        return self.total_bytes / 1_048_576

    @property
    def large(self) -> bool:
        return self.level_mb >= LARGE_LEVEL_MB

    def inactive(
        self, *, days: int = INACTIVE_DAYS, now: datetime | None = None
    ) -> list[PlayerSave]:
        """Players palctl has seen, and hasn't seen for `days`.

        Only players it can name. See rule 1 in the module docstring: a save
        file with no session history is unknown, not idle.
        """
        moment = now or datetime.now(UTC)
        out = [
            p for p in self.players
            if p.known and (p.inactive_for_days(moment) or 0) >= days
        ]
        return sorted(out, key=lambda p: p.last_seen or moment)

    @property
    def unknown(self) -> list[PlayerSave]:
        """Save files palctl cannot match to any session it recorded."""
        return [p for p in self.players if not p.known]

    def reclaimable_bytes(
        self, *, days: int = INACTIVE_DAYS, now: datetime | None = None
    ) -> int:
        """A FLOOR on what cleanup would free — see rule 2. The per-player files
        are small; their records inside Level.sav are the real weight."""
        return sum(p.size_bytes for p in self.inactive(days=days, now=now))


def world_dirs(savegames: Path) -> list[Path]:
    """Every world folder under a SaveGames directory, largest first.

    A dedicated server normally holds exactly one, but a host that has run more
    than one world keeps them all, and an operator with a 40 GB SaveGames folder
    usually has several — which is itself worth telling them.
    """
    if not savegames.is_dir():
        return []
    worlds = [
        world
        for host in sorted(savegames.iterdir())
        if host.is_dir()
        for world in sorted(host.iterdir())
        if world.is_dir() and (world / LEVEL_SAV).is_file()
    ]
    return sorted(worlds, key=_dir_bytes, reverse=True)


def _dir_bytes(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _parse_seen(raw: str) -> datetime | None:
    """A stored ISO timestamp, always as an aware UTC datetime.

    Rows written before palctl stamped a timezone come back naive; comparing a
    naive to an aware datetime raises, and this runs on the path that decides
    whether somebody's character gets deleted. Assume UTC — which is what the
    daemon has always written — rather than letting it explode or, far worse,
    silently sorting wrong.
    """
    try:
        when = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def audit(
    world: Path, seen: dict[str, tuple[str, str]] | None = None
) -> SaveAudit:
    """Measure one world folder. `seen` is SessionStore.last_seen_by_player_id().

    Filenames are matched case-insensitively: Palworld writes the GUID
    upper-case on disk while the REST API reports it in its own casing, and a
    mismatch here would report every player as unknown — which reads as "palctl
    can't help you" rather than as the bug it is.
    """
    known = {k.upper(): v for k, v in (seen or {}).items()}
    level = world / LEVEL_SAV
    try:
        level_bytes = level.stat().st_size if level.is_file() else 0
    except OSError:
        level_bytes = 0

    players: list[PlayerSave] = []
    players_dir = world / PLAYERS_DIR
    if players_dir.is_dir():
        for f in sorted(players_dir.glob("*.sav")):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            guid = f.stem.upper()
            name, raw_seen = known.get(guid, ("", ""))
            players.append(
                PlayerSave(
                    player_id=f.stem,
                    path=f,
                    size_bytes=size,
                    name=name,
                    last_seen=_parse_seen(raw_seen) if raw_seen else None,
                )
            )

    return SaveAudit(
        world=world,
        level_bytes=level_bytes,
        total_bytes=_dir_bytes(world),
        players=players,
    )


def format_audit(
    a: SaveAudit, *, days: int = INACTIVE_DAYS, now: datetime | None = None
) -> str:
    """The operator-facing report. Written to be read by someone whose server
    has started stalling and who wants to know why."""
    moment = now or datetime.now(UTC)
    lines = [
        f"World: {a.world}",
        f"  Level.sav   {a.level_mb:,.0f} MB"
        + ("   ← large; saves and restarts will be slow" if a.large else ""),
        f"  Whole world {a.total_mb:,.0f} MB across {len(a.players)} player save(s)",
    ]

    idle = a.inactive(days=days, now=moment)
    if idle:
        floor_mb = a.reclaimable_bytes(days=days, now=moment) / 1_048_576
        lines.append(f"\n  {len(idle)} player(s) not seen in {days}+ days:")
        for p in idle[:20]:
            gone = p.inactive_for_days(moment) or 0
            lines.append(f"    {p.name or p.player_id:<24} last seen {gone:,.0f} days ago")
        if len(idle) > 20:
            lines.append(f"    … and {len(idle) - 20} more")
        lines.append(
            f"  Their save files are {floor_mb:,.1f} MB — but that is a floor, not "
            "the prize:\n  their characters, guilds and base camps inside Level.sav "
            "are the weight,\n  and measuring that needs a parser palctl does not "
            "yet ship."
        )
    else:
        lines.append(f"\n  No players inactive for {days}+ days.")

    if a.unknown:
        lines.append(
            f"\n  {len(a.unknown)} save file(s) palctl can't match to a player it "
            "has seen.\n  That is 'unknown', not 'unwanted' — palctl only started "
            "recording player\n  GUIDs recently, and a world older than that (or "
            "restored from backup) is\n  full of players it never met. Nothing "
            "here should be deleted on this basis."
        )
    return "\n".join(lines)
