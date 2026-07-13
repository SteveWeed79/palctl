from pathlib import Path

import pytest

from palctl import backups


def make_savegames(tmp_path: Path) -> Path:
    sg = tmp_path / "SaveGames"
    (sg / "0" / "world").mkdir(parents=True)
    (sg / "0" / "world" / "Level.sav").write_bytes(b"x" * 1024)
    return sg


def test_create_and_listing(tmp_path: Path):
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"

    b = backups.create(sg, root, "manual")
    assert b.path.exists()
    assert b.name.endswith("-manual")
    assert (b.path / "0" / "world" / "Level.sav").exists()

    listed = backups.listing(root)
    assert [x.name for x in listed] == [b.name]


def test_create_missing_savegames_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        backups.create(tmp_path / "nope", tmp_path / "backups")


def test_restore_snapshots_current_world(tmp_path: Path):
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")

    # Corrupt the live world, then restore.
    (sg / "0" / "world" / "Level.sav").write_bytes(b"corrupt")
    backups.restore(root, b.name, sg)

    assert (sg / "0" / "world" / "Level.sav").read_bytes() == b"x" * 1024
    pre = [d for d in root.iterdir() if d.name.endswith("-pre-restore")]
    assert len(pre) == 1  # the corrupted world was snapshotted, not lost


@pytest.mark.parametrize("name", ["../../etc", "a/b", "a\\b", "..", "x/../y"])
def test_restore_and_delete_reject_traversal(tmp_path: Path, name: str):
    root = tmp_path / "backups"
    root.mkdir()
    with pytest.raises(ValueError):
        backups.restore(root, name, tmp_path / "SaveGames")
    with pytest.raises(ValueError):
        backups.delete(root, name)


def test_prune_keeps_newest_and_pre_restore(tmp_path: Path):
    root = tmp_path / "backups"
    root.mkdir()
    for stamp in ("2026-01-01_00-00-00", "2026-01-02_00-00-00", "2026-01-03_00-00-00"):
        (root / f"{stamp}-scheduled").mkdir()
    (root / "2026-01-01_12-00-00-pre-restore").mkdir()

    doomed = backups.prune(root, retain=2)

    assert doomed == ["2026-01-01_00-00-00-scheduled"]
    remaining = {d.name for d in root.iterdir()}
    assert "2026-01-01_12-00-00-pre-restore" in remaining
    assert len(remaining) == 3
