"""Backups that can be trusted: manifests, verification, and the hot-copy fix.

Every test here fails against the pre-fix source. The theme is the one the
competitive review kept turning up — a backup that reports success while being
less useful than it claims: an unflushed world filed as clean, a SQLite file
copied mid-write, a mirror that dropped files with nothing to compare against.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from palctl import backups


def world(tmp_path: Path, *, name: str = "SaveGames") -> Path:
    """A minimal savegames tree with a plausible Level.sav."""
    sg = tmp_path / name
    (sg / "0" / "ABC123").mkdir(parents=True)
    (sg / "0" / "ABC123" / "Level.sav").write_bytes(_sav_bytes(b"world-data"))
    (sg / "0" / "ABC123" / "LevelMeta.sav").write_bytes(_sav_bytes(b"meta"))
    return sg


def _sav_bytes(payload: bytes) -> bytes:
    """A 12-byte Palworld save header (uncompressed len, compressed len, magic,
    format byte) followed by `payload`."""
    return (
        len(payload).to_bytes(4, "little")
        + len(payload).to_bytes(4, "little")
        + b"PlZ"
        + b"\x31"
        + payload
    )


# ---------------- the .sav header check ----------------


def test_sav_problem_is_quiet_about_a_healthy_save(tmp_path):
    f = tmp_path / "Level.sav"
    f.write_bytes(_sav_bytes(b"x" * 500))
    assert backups._sav_problem(f) is None


def test_sav_problem_catches_an_empty_file(tmp_path):
    f = tmp_path / "Level.sav"
    f.write_bytes(b"")
    assert "empty" in backups._sav_problem(f)


def test_sav_problem_catches_a_truncated_save(tmp_path):
    """The failure a size check alone misses: the header says how much payload
    should follow, and it doesn't."""
    f = tmp_path / "Level.sav"
    f.write_bytes(_sav_bytes(b"x" * 500)[:200])
    problem = backups._sav_problem(f)
    assert "truncated" in problem and "500" in problem


def test_sav_problem_stays_silent_on_a_format_it_does_not_know(tmp_path):
    """A check that cries wolf about a format Pocketpair changed in a patch is
    worse than no check — the next real warning gets ignored too."""
    f = tmp_path / "Level.sav"
    f.write_bytes(b"\x00" * 64)  # no PlZ magic
    assert backups._sav_problem(f) is None


# ---------------- the manifest ----------------


def test_create_records_a_manifest(tmp_path):
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")

    manifest = json.loads((b.path / backups.MANIFEST_NAME).read_text())
    assert manifest["file_count"] == 2
    assert manifest["total_bytes"] > 0
    assert not manifest["problems_at_creation"]
    # Its own bookkeeping is never counted as world data.
    assert all(
        name not in manifest["files"] for name in backups.PASSENGER_FILES
    )


def test_manifest_records_whether_the_save_flushed(tmp_path):
    """The whole point of threading `flushed` down: a restore can say the world
    may be older than the backup's timestamp."""
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test", flushed=False)
    assert backups.read_manifest(tmp_path / "backups", b.name)["flushed"] is False


def test_manifest_flushed_is_none_when_nobody_said(tmp_path):
    """Distinct from False: 'not told' is not 'the save failed'."""
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")
    assert backups.read_manifest(tmp_path / "backups", b.name)["flushed"] is None


def test_read_manifest_returns_none_for_a_backup_without_one(tmp_path):
    root = tmp_path / "backups"
    (root / "2026-01-01_00-00-00-legacy").mkdir(parents=True)
    assert backups.read_manifest(root, "2026-01-01_00-00-00-legacy") is None


# ---------------- verify ----------------


def test_verify_passes_a_fresh_backup(tmp_path):
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")

    report = backups.verify(tmp_path / "backups", b.name)
    assert report.ok
    assert report.problems == []
    assert report.checked == 2
    assert not report.unmanifested


def test_verify_catches_a_file_the_mirror_dropped(tmp_path):
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")
    (b.path / "0" / "ABC123" / "LevelMeta.sav").unlink()

    report = backups.verify(tmp_path / "backups", b.name)
    assert not report.ok
    assert any("missing" in p for p in report.problems)


def test_verify_catches_a_short_copy(tmp_path):
    """Size drift against the manifest — a half-written file that no exit code
    ever reported."""
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")
    target = b.path / "0" / "ABC123" / "Level.sav"
    target.write_bytes(target.read_bytes()[:6])

    report = backups.verify(tmp_path / "backups", b.name)
    assert not report.ok
    assert any("Level.sav" in p for p in report.problems)


def test_verify_reports_an_unflushed_backup(tmp_path):
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test", flushed=False)

    report = backups.verify(tmp_path / "backups", b.name)
    assert not report.ok
    assert any("did not complete" in p for p in report.problems)


def test_verify_says_when_there_is_no_manifest_to_check_against(tmp_path):
    """A backup taken before manifests existed still gets the file-level checks,
    but callers must be able to say 'looks intact', not 'verified'."""
    root = tmp_path / "backups"
    d = root / "2026-01-01_00-00-00-legacy"
    d.mkdir(parents=True)
    (d / "Level.sav").write_bytes(_sav_bytes(b"data"))

    report = backups.verify(root, "2026-01-01_00-00-00-legacy")
    assert report.ok
    assert report.unmanifested


def test_verify_rejects_a_directory_with_no_world_in_it(tmp_path):
    root = tmp_path / "backups"
    d = root / "2026-01-01_00-00-00-empty"
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("hello")

    report = backups.verify(root, "2026-01-01_00-00-00-empty")
    assert not report.ok
    assert any("does not look like a world" in p for p in report.problems)


def test_verify_refuses_a_traversing_name(tmp_path):
    report = backups.verify(tmp_path / "backups", "../../etc")
    assert not report.ok


def test_verify_reports_a_missing_backup_rather_than_raising(tmp_path):
    report = backups.verify(tmp_path / "backups", "2026-01-01_00-00-00-gone")
    assert not report.ok
    assert any("missing" in p for p in report.problems)


# ---------------- the SQLite hot-copy fix ----------------


def test_config_snapshot_holds_an_openable_database(tmp_path, monkeypatch):
    """A live SQLite file copied byte-for-byte can land invalid. The snapshot
    goes through the backup API, so what's in the zip always opens."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{}")

    db = cfg_dir / "sessions.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (player TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('zoe')")
    conn.commit()
    # Deliberately left OPEN with an uncommitted write in flight — the exact
    # situation a hot file copy can capture mid-transaction.
    conn.execute("INSERT INTO sessions VALUES ('half-written')")

    monkeypatch.setattr("palctl.config.config_dir", lambda: cfg_dir)

    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")

    with zipfile.ZipFile(b.path / backups.CONFIG_SNAPSHOT_NAME) as z:
        z.extract("sessions.db", tmp_path / "out")
    restored = sqlite3.connect(tmp_path / "out" / "sessions.db")
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()


# ---------------- the passenger files stay out of the world ----------------


def test_restore_leaves_palctl_bookkeeping_out_of_savegames(tmp_path):
    """A manifest restored into SaveGames would be a palctl file sitting in the
    game's world folder. The config snapshot was already excluded; the manifest
    must ride the same list."""
    sg = world(tmp_path)
    b = backups.create(sg, tmp_path / "backups", "test")

    backups.restore(tmp_path / "backups", b.name, sg)

    assert not (sg / backups.MANIFEST_NAME).exists()
    assert not (sg / backups.CONFIG_SNAPSHOT_NAME).exists()
    assert (sg / "0" / "ABC123" / "Level.sav").exists()


# ---------------- last_backup_at ----------------


def test_last_backup_at_reads_the_newest_stamp(tmp_path):
    root = tmp_path / "backups"
    for name in (
        "2026-01-01_03-00-00-scheduled",
        "2026-03-04_17-45-01-manual",
        "2026-02-01_00-00-00-scheduled",
    ):
        (root / name).mkdir(parents=True)

    assert backups.last_backup_at(root) == datetime(2026, 3, 4, 17, 45, 1)


def test_last_backup_at_is_none_when_there_are_none(tmp_path):
    assert backups.last_backup_at(tmp_path / "backups") is None


def test_last_backup_at_ignores_a_directory_that_is_not_ours(tmp_path):
    root = tmp_path / "backups"
    (root / "my-own-folder").mkdir(parents=True)
    (root / "2026-01-01_03-00-00-scheduled").mkdir()
    assert backups.last_backup_at(root) == datetime(2026, 1, 1, 3, 0, 0)


def test_last_backup_at_skips_an_unfinished_copy(tmp_path):
    """A `.partial` is an interrupted copy, not a backup — counting it would
    push the next scheduled backup out by a whole interval on the strength of
    a copy that never landed."""
    root = tmp_path / "backups"
    (root / "2026-01-01_03-00-00-scheduled").mkdir(parents=True)
    (root / "2026-06-01_03-00-00-scheduled.partial").mkdir()
    assert backups.last_backup_at(root) == datetime(2026, 1, 1, 3, 0, 0)


# ---------------- same_volume ----------------


def test_same_volume_spots_backups_on_the_server_disk(tmp_path):
    a = tmp_path / "server"
    b = tmp_path / "backups"
    a.mkdir()
    b.mkdir()
    assert backups.same_volume(a, b) is True


def test_same_volume_is_none_when_it_cannot_tell(tmp_path):
    assert backups.same_volume(tmp_path / "nope", tmp_path) is None


@pytest.mark.parametrize("age_days,expected", [(0, True), (9, True)])
def test_last_backup_at_is_stable_across_ages(tmp_path, age_days, expected):
    """Guard test: the stamp is parsed from the name, so it does not drift with
    the directory's mtime the way a filesystem timestamp would."""
    root = tmp_path / "backups"
    stamp = (datetime(2026, 5, 1, 12, 0, 0) - timedelta(days=age_days)).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    (root / f"{stamp}-scheduled").mkdir(parents=True)
    assert (backups.last_backup_at(root) is not None) is expected
