"""Diagnosing save bloat without touching a save.

The two properties that matter most here are refusals, not features: an unknown
player must never be reported as an idle one, and the reclaimable figure must
never be presented as more than the floor it is. Both exist because the next
step after this module is deleting somebody's character.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from palctl import saveaudit
from palctl.saveaudit import audit, format_audit, world_dirs

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def make_world(
    tmp_path: Path, *, guids: dict[str, int] | None = None, level_mb: float = 1.0
) -> Path:
    world = tmp_path / "SaveGames" / "0" / "ABCDEF0123456789"
    (world / "Players").mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"x" * int(level_mb * 1_048_576))
    (world / "LevelMeta.sav").write_bytes(b"meta")
    for guid, size in (guids or {}).items():
        (world / "Players" / f"{guid}.sav").write_bytes(b"p" * size)
    return world


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# ---------------- finding worlds ----------------


def test_world_dirs_finds_a_world(tmp_path):
    world = make_world(tmp_path)
    assert world_dirs(tmp_path / "SaveGames") == [world]


def test_a_directory_without_a_level_sav_is_not_a_world(tmp_path):
    sg = tmp_path / "SaveGames" / "0" / "notaworld"
    sg.mkdir(parents=True)
    assert world_dirs(tmp_path / "SaveGames") == []


def test_worlds_are_reported_largest_first(tmp_path):
    """An operator with a 40 GB SaveGames folder usually has several worlds,
    and the big one is the one they came to ask about."""
    small = tmp_path / "SaveGames" / "0" / "small"
    big = tmp_path / "SaveGames" / "0" / "big"
    for d, size in ((small, 1000), (big, 50_000)):
        d.mkdir(parents=True)
        (d / "Level.sav").write_bytes(b"x" * size)

    assert world_dirs(tmp_path / "SaveGames") == [big, small]


def test_a_missing_savegames_folder_is_not_an_error(tmp_path):
    assert world_dirs(tmp_path / "nope") == []


# ---------------- measurement ----------------


def test_audit_measures_the_level_and_the_whole_world(tmp_path):
    world = make_world(tmp_path, guids={"AAAA": 100}, level_mb=2)
    a = audit(world)

    assert a.level_mb == pytest.approx(2, abs=0.01)
    assert a.total_bytes > a.level_bytes  # the player save and meta count too
    assert len(a.players) == 1


def test_a_big_level_is_flagged(tmp_path):
    world = make_world(tmp_path, level_mb=0.1)
    assert not audit(world).large

    monkey = audit(world)
    object.__setattr__(monkey, "level_bytes", int(saveaudit.LARGE_LEVEL_MB) * 1_048_576)
    assert monkey.large


# ---------------- the two honesty rules ----------------


def test_a_player_with_no_session_history_is_unknown_not_inactive(tmp_path):
    """Rule 1. palctl only started recording player GUIDs recently, and a world
    restored from a backup is full of players it never met. Reporting those as
    inactive would aim a cleanup straight at them."""
    world = make_world(tmp_path, guids={"NEVERSEEN": 500})

    a = audit(world, seen={})

    assert a.inactive(now=NOW) == []
    assert len(a.unknown) == 1
    assert a.reclaimable_bytes(now=NOW) == 0


def test_reclaimable_counts_only_the_player_files(tmp_path):
    """Rule 2: it is a floor. The records inside Level.sav are the real weight,
    and this module refuses to guess at them."""
    world = make_world(tmp_path, guids={"OLD": 1000}, level_mb=5)

    a = audit(world, seen={"OLD": ("zoe", iso(200))})

    assert a.reclaimable_bytes(now=NOW) == 1000  # not a share of Level.sav


def test_an_inactive_player_is_found_with_their_name(tmp_path):
    world = make_world(tmp_path, guids={"OLD": 700})

    idle = audit(world, seen={"OLD": ("zoe", iso(120))}).inactive(now=NOW)

    assert [p.name for p in idle] == ["zoe"]
    assert idle[0].inactive_for_days(NOW) == pytest.approx(120, abs=0.1)


def test_a_recent_player_is_left_alone(tmp_path):
    world = make_world(tmp_path, guids={"ACTIVE": 700})

    a = audit(world, seen={"ACTIVE": ("ana", iso(3))})

    assert a.inactive(now=NOW) == []
    assert a.unknown == []  # known, just not idle


def test_the_inactivity_threshold_is_the_boundary(tmp_path):
    world = make_world(tmp_path, guids={"EDGE": 10})
    seen = {"EDGE": ("edge", iso(90))}

    assert len(audit(world, seen=seen).inactive(days=90, now=NOW)) == 1
    assert audit(world, seen=seen).inactive(days=91, now=NOW) == []


def test_save_filenames_match_regardless_of_case(tmp_path):
    """Palworld writes the GUID upper-case on disk; the REST API reports its
    own casing. A mismatch would report every player as unknown, which reads as
    "palctl can't help you" rather than as the bug it is."""
    world = make_world(tmp_path, guids={"abcdef123": 400})

    a = audit(world, seen={"ABCDEF123": ("zoe", iso(200))})

    assert [p.name for p in a.inactive(now=NOW)] == ["zoe"]


def test_a_naive_stored_timestamp_does_not_explode(tmp_path):
    """Old rows were written without a timezone. Comparing naive to aware
    raises — on the code path that decides whether a character gets deleted."""
    world = make_world(tmp_path, guids={"OLD": 10})
    naive = (NOW - timedelta(days=200)).replace(tzinfo=None).isoformat()

    idle = audit(world, seen={"OLD": ("zoe", naive)}).inactive(now=NOW)

    assert len(idle) == 1


def test_an_unparseable_timestamp_reads_as_unknown(tmp_path):
    world = make_world(tmp_path, guids={"ODD": 10})

    a = audit(world, seen={"ODD": ("zoe", "not-a-date")})

    assert a.inactive(now=NOW) == []
    assert len(a.unknown) == 1


def test_players_still_online_are_never_inactive(tmp_path):
    """last_seen_by_player_id uses the newest of joined_at/left_at, so someone
    online right now reads as active rather than as "never left"."""
    world = make_world(tmp_path, guids={"ONLINE": 10})

    a = audit(world, seen={"ONLINE": ("ana", iso(0))})

    assert a.inactive(now=NOW) == []


# ---------------- the report ----------------


def test_the_report_names_idle_players_and_qualifies_the_number(tmp_path):
    world = make_world(tmp_path, guids={"OLD": 2_000_000}, level_mb=1)

    text = format_audit(audit(world, seen={"OLD": ("zoe", iso(150))}), now=NOW)

    assert "zoe" in text
    assert "150 days ago" in text
    assert "floor" in text  # never presented as the whole prize


def test_the_report_refuses_to_call_unknown_saves_unwanted(tmp_path):
    world = make_world(tmp_path, guids={"MYSTERY": 100})

    text = format_audit(audit(world, seen={}), now=NOW)

    assert "'unknown', not 'unwanted'" in text
    assert "Nothing here should be deleted on this basis" in text


def test_a_healthy_world_says_so_plainly(tmp_path):
    world = make_world(tmp_path, guids={"ACTIVE": 100})

    text = format_audit(audit(world, seen={"ACTIVE": ("ana", iso(1))}), now=NOW)

    assert "No players inactive" in text


def test_the_audit_never_writes_to_the_world(tmp_path):
    """The whole point of this module being read-only."""
    world = make_world(tmp_path, guids={"OLD": 100})
    before = {p: p.stat().st_mtime_ns for p in world.rglob("*") if p.is_file()}

    format_audit(audit(world, seen={"OLD": ("zoe", iso(300))}), now=NOW)

    after = {p: p.stat().st_mtime_ns for p in world.rglob("*") if p.is_file()}
    assert before == after
