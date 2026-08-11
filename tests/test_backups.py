import shutil
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
    assert b.consistent is True  # nothing wrote to the source during the copy

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


@pytest.mark.parametrize("name", ["", " ", "."])
def test_restore_and_delete_reject_empty_and_dot(tmp_path: Path, name: str):
    # "" and "." both collapse to backup_root itself; without the guard restore
    # would copy the whole backups folder over the world and delete would rmtree
    # every backup at once.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    backups.create(sg, root, "manual")
    with pytest.raises(ValueError):
        backups.restore(root, name, sg)
    with pytest.raises(ValueError):
        backups.delete(root, name)
    # The guard must not have touched anything.
    assert (sg / "0" / "world" / "Level.sav").exists()
    assert len([d for d in root.iterdir() if d.is_dir()]) == 1


def test_is_restorable(tmp_path: Path):
    root = tmp_path / "backups"
    (root / "2026-01-01_00-00-00-manual").mkdir(parents=True)
    assert backups.is_restorable(root, "2026-01-01_00-00-00-manual") is True
    for bad in ("", " ", ".", "..", "nope", "../etc", "a/b"):
        assert backups.is_restorable(root, bad) is False


def test_mirror_copies_backup_and_is_idempotent(tmp_path: Path):
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")

    mirror_root = tmp_path / "mirror"
    dest = backups.mirror(b.path, mirror_root)
    assert dest == mirror_root / b.name
    assert (dest / "0" / "world" / "Level.sav").read_bytes() == b"x" * 1024

    # A retry must not blow up on the existing copy.
    assert backups.mirror(b.path, mirror_root) == dest
    # Same layout as backup_root, so listing/prune work on the mirror too.
    assert [x.name for x in backups.listing(mirror_root)] == [b.name]


def test_test_mirror_local_path_ok_and_creates_it(tmp_path: Path):
    target = tmp_path / "mirror" / "sub"  # doesn't exist yet
    ok, _msg = backups.test_mirror(str(target))
    assert ok is True
    assert target.is_dir()  # created for the test
    assert not (target / ".palctl-write-test").exists()  # probe cleaned up


def test_test_mirror_local_path_not_writable(tmp_path: Path):
    # A file where a directory should be: mkdir under it fails (NotADirectoryError).
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    ok, msg = backups.test_mirror(str(blocker / "mirror"))
    assert ok is False
    assert "Not writable" in msg


def test_test_mirror_empty_target(tmp_path: Path):
    ok, _msg = backups.test_mirror("   ")
    assert ok is False


def test_test_mirror_delegates_remotes_to_rclone(monkeypatch):
    from palctl import rclone

    called: list = []
    monkeypatch.setattr(rclone, "test_remote",
                        lambda t: called.append(t) or (True, "ok"))
    ok, _msg = backups.test_mirror("gdrive:PalworldBackups")
    assert ok is True and called == ["gdrive:PalworldBackups"]


def test_listing_ignores_partial_copies(tmp_path: Path):
    # A mirror mid-copy leaves a "<name>.partial" dir; it must not be listed (or
    # restored/pruned) as if it were a finished backup.
    root = tmp_path / "backups"
    (root / "2026-01-02_00-00-00-manual").mkdir(parents=True)
    (root / "2026-01-01_00-00-00-manual.partial").mkdir()
    assert [b.name for b in backups.listing(root)] == ["2026-01-02_00-00-00-manual"]


def test_create_interrupted_leaves_no_fake_backup(tmp_path: Path, monkeypatch):
    # A daemon kill / disk-full mid-copy must not leave a directory that
    # listing() (and therefore restore/prune) would treat as a finished backup.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"

    real_copytree = backups.shutil.copytree

    def dies_mid_copy(src, dst, *a, **kw):
        real_copytree(src, dst, *a, **kw)  # the files land...
        raise OSError("No space left on device")  # ...but the copy "fails"

    monkeypatch.setattr(backups.shutil, "copytree", dies_mid_copy)
    with pytest.raises(OSError):
        backups.create(sg, root, "scheduled")

    assert backups.listing(root) == []  # nothing masquerades as complete
    assert not any(root.iterdir())  # and the .partial temp was cleaned up


def test_restore_failure_leaves_live_world_untouched(tmp_path: Path, monkeypatch):
    # If copying the backup out fails (disk full, unreadable backup), the live
    # world must not have been touched yet — no rmtree-then-hope.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")
    (sg / "0" / "world" / "Level.sav").write_bytes(b"live-world")

    def dies(src, dst, **kw):
        raise OSError("No space left on device")

    monkeypatch.setattr(backups.shutil, "copytree", dies)
    with pytest.raises(OSError):
        backups.restore(root, b.name, sg)

    assert (sg / "0" / "world" / "Level.sav").read_bytes() == b"live-world"
    assert not (sg.parent / f"{sg.name}.partial-restore").exists()


def test_restore_never_leaves_the_world_missing_if_archiving_fails(tmp_path: Path):
    """The world is swapped in before the undo copy is archived, so the slow,
    failure-prone half can't cost you the world.

    This is the ordering bug the old code had: it copied the live world to the
    backup folder *first* (minutes, and it fails on a full backup volume) and
    rmtree'd the live world before moving the restored copy in. Any failure in
    that window left SaveGames gone — and the caller then started the server,
    which had Palworld generate a fresh world over the top of the problem."""
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")
    (sg / "0" / "world" / "Level.sav").write_bytes(b"live-world")

    # Archiving the swapped-out world fails — a full backup volume, a share
    # that dropped, a permissions problem.
    def archive_dies(src, dest):
        raise OSError("No space left on device")

    original = backups._copytree_staged
    backups._copytree_staged = archive_dies
    try:
        warning = backups.restore(root, b.name, sg)
    finally:
        backups._copytree_staged = original

    # The restore itself SUCCEEDED — the live world is the backup's contents.
    assert (sg / "0" / "world" / "Level.sav").read_bytes() == b"x" * 1024
    # ...and it says so, rather than reporting a failure that didn't happen.
    assert warning and "succeeded" in warning
    # The old world is still recoverable, sitting where the caller is told.
    aside = sg.parent / f"{sg.name}.pre-restore"
    assert (aside / "0" / "world" / "Level.sav").read_bytes() == b"live-world"


def test_restore_reports_no_warning_on_a_clean_run(tmp_path: Path):
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")
    assert backups.restore(root, b.name, sg) is None
    # The swapped-out world was archived and its working copy cleaned up.
    assert not (sg.parent / f"{sg.name}.pre-restore").exists()
    assert len([d for d in root.iterdir() if d.name.endswith("-pre-restore")]) == 1


def test_restore_onto_a_missing_world_needs_no_undo_copy(tmp_path: Path):
    # A world that doesn't exist yet (fresh install, or a previous restore that
    # was interrupted) has nothing to snapshot — and must not fail over it.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")
    shutil.rmtree(sg)

    assert backups.restore(root, b.name, sg) is None
    assert (sg / "0" / "world" / "Level.sav").read_bytes() == b"x" * 1024
    assert not any(d.name.endswith("-pre-restore") for d in root.iterdir())


def test_create_retries_once_for_a_quiet_window(tmp_path: Path, monkeypatch):
    # Backups are usually HOT — the server keeps running. If it writes a .sav
    # while the copy runs, that attempt may be torn; create() must notice (the
    # source fingerprint changed under it) and retry, and the retry lands clean.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"

    real_copytree = backups.shutil.copytree
    attempts: list[int] = []

    def server_writes_during_first_copy(src, dst, *a, **kw):
        # copytree recurses through the module-level name, so the wrapper fires
        # for every subdirectory too — only the top-level call is an "attempt".
        if Path(src) != sg:
            return real_copytree(src, dst, *a, **kw)
        attempts.append(1)
        result = real_copytree(src, dst, *a, **kw)
        if len(attempts) == 1:  # mid-copy autosave, first attempt only
            (sg / "0" / "world" / "Level.sav").write_bytes(b"y" * 2048)
        return result

    monkeypatch.setattr(backups.shutil, "copytree", server_writes_during_first_copy)
    b = backups.create(sg, root, "scheduled")

    assert len(attempts) == 2  # dirty attempt discarded, clean retry taken
    assert b.consistent is True
    # The backup is the retry's snapshot — the post-write world, intact.
    assert (b.path / "0" / "world" / "Level.sav").read_bytes() == b"y" * 2048


def test_create_keeps_but_flags_a_never_quiet_backup(tmp_path: Path, monkeypatch):
    # If the server writes through every attempt, the last copy is kept anyway
    # (a probably-fine backup beats none) but flagged, so the scheduler can
    # warn and a restore can prefer a clean neighbour.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"

    real_copytree = backups.shutil.copytree
    counter = iter(range(100))

    def server_never_stops_writing(src, dst, *a, **kw):
        result = real_copytree(src, dst, *a, **kw)
        (sg / "0" / "world" / "Level.sav").write_bytes(b"z%d" % next(counter) * 512)
        return result

    monkeypatch.setattr(backups.shutil, "copytree", server_never_stops_writing)
    b = backups.create(sg, root, "scheduled", consistency_retries=2)

    assert b.consistent is False
    assert b.path.exists()  # kept, not discarded
    assert [x.name for x in backups.listing(root)] == [b.name]


def test_create_flags_a_torn_copy_even_when_source_looks_quiet(tmp_path: Path, monkeypatch):
    # The other tear: the copy itself is short (a file the server replaced
    # mid-read) while the source fingerprint happens to match afterwards.
    # _copy_matches compares the copied sizes against the pre-copy fingerprint
    # and must flag it.
    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"

    real_copytree = backups.shutil.copytree

    def truncates_the_copy(src, dst, *a, **kw):
        result = real_copytree(src, dst, *a, **kw)
        if Path(src) == sg:  # only after the top-level copy finished
            (Path(dst) / "0" / "world" / "Level.sav").write_bytes(b"short")
        return result

    monkeypatch.setattr(backups.shutil, "copytree", truncates_the_copy)
    b = backups.create(sg, root, "scheduled", consistency_retries=1)

    assert b.consistent is False
    assert b.path.exists()


def test_prune_retain_zero_never_wipes_everything(tmp_path: Path):
    # backup_retain: 0 in a hand-edited config must not mean "delete all" —
    # prune runs immediately after every create.
    root = tmp_path / "backups"
    root.mkdir()
    for stamp in ("2026-01-01_00-00-00", "2026-01-02_00-00-00"):
        (root / f"{stamp}-scheduled").mkdir()

    doomed = backups.prune(root, retain=0)

    assert doomed == ["2026-01-01_00-00-00-scheduled"]
    assert (root / "2026-01-02_00-00-00-scheduled").exists()


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


def test_prune_never_deletes_non_backup_dirs(tmp_path: Path):
    # Symmetric with the rclone mirror: a local mirror pointed at a populated
    # location (another disk's root, a shared network folder) must only ever
    # prune palctl's own backups — never the user's unrelated directories, even
    # when those sort above or below the timestamped backups.
    root = tmp_path / "shared_disk"
    root.mkdir()
    (root / "Photos").mkdir()          # sorts above the timestamps
    (root / "0-user-folder").mkdir()   # sorts below the timestamps
    for stamp in ("2026-01-01_00-00-00", "2026-01-02_00-00-00", "2026-01-03_00-00-00"):
        (root / f"{stamp}-scheduled").mkdir()

    doomed = backups.prune(root, retain=1)

    assert doomed == ["2026-01-02_00-00-00-scheduled", "2026-01-01_00-00-00-scheduled"]
    remaining = {d.name for d in root.iterdir()}
    assert "Photos" in remaining and "0-user-folder" in remaining  # untouched
    assert "2026-01-03_00-00-00-scheduled" in remaining  # newest backup kept


# ---------- config snapshot (palctl's own DR, riding inside each backup) ----------


def test_create_writes_config_snapshot(tmp_path: Path, monkeypatch):
    # The world was covered; the config that manages it wasn't. Every backup now
    # carries palctl-config.zip with the config/state/history — whitelisted, so
    # the token (a local secret) and the logs never ride a backup off-box.
    import zipfile

    cfgdir = tmp_path / "palctl-config"
    cfgdir.mkdir()
    (cfgdir / "config.json").write_text('{"api_port": 8212}', encoding="utf-8")
    (cfgdir / "sessions.db").write_bytes(b"sqlite-ish")
    (cfgdir / "daemon_token").write_text("SECRET", encoding="utf-8")
    (cfgdir / "daemon_state.json").write_text('{"desired_running": true}', encoding="utf-8")
    monkeypatch.setattr("palctl.config.config_dir", lambda: cfgdir)

    b = backups.create(make_savegames(tmp_path), tmp_path / "backups", "manual")

    snap = b.path / backups.CONFIG_SNAPSHOT_NAME
    assert snap.exists()
    with zipfile.ZipFile(snap) as z:
        names = set(z.namelist())
    assert "config.json" in names
    assert "sessions.db" in names
    assert "daemon_state.json" in names
    assert "daemon_token" not in names  # a local secret never leaves the box


def test_create_survives_missing_config(tmp_path: Path, monkeypatch):
    # A snapshot problem must never fail the world backup — the world is the point.
    monkeypatch.setattr("palctl.config.config_dir", lambda: tmp_path / "missing")
    b = backups.create(make_savegames(tmp_path), tmp_path / "backups", "manual")
    assert b.path.exists()  # backup fine regardless


def test_restore_excludes_the_config_snapshot(tmp_path: Path, monkeypatch):
    # The snapshot rides inside the backup dir (so retention/mirroring cover
    # it), but it is palctl's file — restoring must not plant a zip in SaveGames.
    cfgdir = tmp_path / "palctl-config"
    cfgdir.mkdir()
    (cfgdir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("palctl.config.config_dir", lambda: cfgdir)

    sg = make_savegames(tmp_path)
    root = tmp_path / "backups"
    b = backups.create(sg, root, "manual")
    assert (b.path / backups.CONFIG_SNAPSHOT_NAME).exists()

    backups.restore(root, b.name, sg)
    assert not (sg / backups.CONFIG_SNAPSHOT_NAME).exists()
    assert (sg / "0" / "world" / "Level.sav").exists()  # the world came back


def _mkbackup(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "Level.sav").write_bytes(b"world")
    return d


def test_pre_restore_copies_are_bounded_not_unlimited(tmp_path):
    """They used to be exempt from retention entirely. That reads as safe and
    isn't: each one is a full copy of the world, taken automatically on every
    restore, piling up on the same disk as the live one — and a full disk
    corrupts saves, which is why palctl warns about low space at all."""
    root = tmp_path / "backups"
    for i in range(6):
        _mkbackup(root, f"2026-08-0{i + 1}_10-00-00-pre-restore")
    _mkbackup(root, "2026-08-09_10-00-00-scheduled")

    backups.prune(root, retain=5)

    left = sorted(p.name for p in root.iterdir())
    kept = [n for n in left if n.endswith("-pre-restore")]
    assert len(kept) == backups.PRE_RESTORE_RETAIN
    # The newest ones survive — undoing the most recent restore still works.
    assert kept == sorted(kept, reverse=True)[::-1]
    assert "2026-08-06_10-00-00-pre-restore" in kept
    assert "2026-08-01_10-00-00-pre-restore" not in kept


def test_pre_restore_copies_do_not_eat_the_ordinary_retention_budget(tmp_path):
    """Counted separately, so a restore's safety copy can never push a real
    backup out — nor be pushed out by one."""
    root = tmp_path / "backups"
    for i in range(3):
        _mkbackup(root, f"2026-08-0{i + 1}_10-00-00-scheduled")
    _mkbackup(root, "2026-08-04_10-00-00-pre-restore")

    backups.prune(root, retain=3)

    left = sorted(p.name for p in root.iterdir())
    assert len([n for n in left if n.endswith("-scheduled")]) == 3
    assert "2026-08-04_10-00-00-pre-restore" in left


def test_prune_still_ignores_directories_that_are_not_ours(tmp_path):
    root = tmp_path / "backups"
    _mkbackup(root, "2026-08-01_10-00-00-scheduled")
    _mkbackup(root, "2026-08-02_10-00-00-scheduled")
    mine = root / "my important folder"
    mine.mkdir(parents=True)

    backups.prune(root, retain=1)

    assert mine.exists(), "a populated backup root must never lose the user's data"
