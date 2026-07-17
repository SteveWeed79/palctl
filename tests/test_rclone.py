from pathlib import Path

import pytest

from palctl import rclone


@pytest.mark.parametrize(
    "target,expected",
    [
        ("gdrive:PalworldBackups", True),
        ("gdrive:", True),
        ("dropbox:games/pal", True),
        ("s3-backup:bucket/path", True),
        # A single-letter name before the colon is a Windows drive, not a remote.
        (r"D:\PalworldBackups", False),
        ("C:/Users/steve/Backups", False),
        # Plain local paths.
        ("/mnt/backups", False),
        (r"\\nas\share\pal", False),
        ("", False),
        ("relative/dir", False),
    ],
)
def test_is_remote(target: str, expected: bool):
    assert rclone.is_remote(target) is expected


def test_join_handles_root_and_subpath():
    assert rclone._join("gdrive:", "b1") == "gdrive:b1"
    assert rclone._join("gdrive:Pal", "b1") == "gdrive:Pal/b1"
    assert rclone._join("gdrive:Pal/", "b1") == "gdrive:Pal/b1"


class FakeRclone:
    """Stand-in for the rclone binary: records argv, replays canned output."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.lsf_output = ""
        self.fail_on: str | None = None  # subcommand to fail (returncode 1)
        self.timeout_on: str | None = None  # subcommand to hang (TimeoutExpired)

    def which(self, name):
        return "/usr/bin/rclone" if name == "rclone" else None

    def run(self, argv, capture_output, text, timeout=None):
        import subprocess

        self.calls.append(argv[1:])  # drop the rclone exe path; keep the args
        sub = argv[1]  # argv[0] is the rclone path
        if self.timeout_on == sub:
            raise subprocess.TimeoutExpired(argv, timeout or 0)
        if self.fail_on == sub:
            return subprocess.CompletedProcess(argv, 1, "", "quota exceeded")
        out = self.lsf_output if sub == "lsf" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def install(self, monkeypatch):
        monkeypatch.setattr(rclone.shutil, "which", self.which)
        monkeypatch.setattr(rclone.subprocess, "run", self.run)
        return self


def test_mirror_copies_to_remote_dest(tmp_path: Path, monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    backup = tmp_path / "2026-07-15_10-00-00-scheduled"
    backup.mkdir()

    dest = rclone.mirror(backup, "gdrive:PalworldBackups")

    assert dest == "gdrive:PalworldBackups/2026-07-15_10-00-00-scheduled"
    assert fake.calls == [
        ["copy", str(backup), "gdrive:PalworldBackups/2026-07-15_10-00-00-scheduled"]
    ]


def test_missing_binary_raises_actionable_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(rclone.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError, match="rclone is not installed"):
        rclone.mirror(tmp_path, "gdrive:Pal")


def test_failed_rclone_surfaces_stderr(tmp_path: Path, monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    fake.fail_on = "copy"
    with pytest.raises(RuntimeError, match="quota exceeded"):
        rclone.mirror(tmp_path, "gdrive:Pal")


def test_listing_parses_and_sorts_newest_first(monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = (
        "2026-07-15_06-00-00-scheduled/\n"
        "2026-07-15_12-00-00-scheduled/\n"
        "2026-07-15_00-00-00-scheduled/\n"
    )
    assert rclone.listing("gdrive:Pal") == [
        "2026-07-15_12-00-00-scheduled",
        "2026-07-15_06-00-00-scheduled",
        "2026-07-15_00-00-00-scheduled",
    ]


def test_prune_purges_all_but_newest_retain(monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = (
        "2026-07-13_00-00-00-scheduled/\n"
        "2026-07-14_00-00-00-scheduled/\n"
        "2026-07-15_00-00-00-scheduled/\n"
    )
    doomed = rclone.prune("gdrive:Pal", retain=2)

    assert doomed == ["2026-07-13_00-00-00-scheduled"]
    purges = [c for c in fake.calls if c[0] == "purge"]
    assert purges == [["purge", "gdrive:Pal/2026-07-13_00-00-00-scheduled"]]


def test_prune_retain_zero_never_wipes_everything(monkeypatch):
    # Mirrors backups.prune: a bad config must read as "keep the latest".
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = "2026-07-14_00-00-00-scheduled/\n2026-07-15_00-00-00-scheduled/\n"
    doomed = rclone.prune("gdrive:Pal", retain=0)

    assert doomed == ["2026-07-14_00-00-00-scheduled"]  # only the oldest


def test_test_remote_lists_the_root_not_the_subpath(monkeypatch):
    # The configured subpath may not exist before the first backup, so the
    # connection test must probe the remote root (auth), not `remote:sub`.
    fake = FakeRclone().install(monkeypatch)
    ok, msg = rclone.test_remote("gdrive:PalworldBackups/nested")

    assert ok is True
    assert "gdrive:" in msg
    assert fake.calls == [["lsd", "gdrive:"]]


def test_test_remote_reports_auth_failure(monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    fake.fail_on = "lsd"
    ok, msg = rclone.test_remote("gdrive:PalworldBackups")

    assert ok is False
    assert "quota exceeded" in msg  # rclone's stderr, surfaced verbatim


def test_prune_never_touches_pre_restore(monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = (
        "2026-07-14_00-00-00-scheduled/\n"
        "2026-07-15_00-00-00-scheduled/\n"
        "2026-07-15_06-00-00-pre-restore/\n"
    )
    doomed = rclone.prune("gdrive:Pal", retain=1)

    assert "2026-07-15_06-00-00-pre-restore" not in doomed
    purged = {c[1] for c in fake.calls if c[0] == "purge"}
    assert all("pre-restore" not in p for p in purged)


@pytest.mark.parametrize(
    "target,expected",
    [
        ("gdrive:PalworldBackups", True),
        ("gdrive:Pal/nested", True),
        ("gdrive:Pal/", True),
        ("gdrive:", False),       # bare remote root — no dedicated folder
        ("gdrive:/", False),      # still the root
        ("gdrive:   ", False),    # whitespace isn't a folder
    ],
)
def test_has_subpath(target: str, expected: bool):
    assert rclone.has_subpath(target) is expected


def test_listing_ignores_non_backup_dirs(monkeypatch):
    # A remote can be a shared folder (or a Drive root) holding the user's own
    # directories. Retention must never see — let alone delete — those.
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = (
        "Photos/\n"
        "Documents/\n"
        "2026-07-15_06-00-00-scheduled/\n"
        "0-random-user-folder/\n"           # sorts *below* the timestamps
        "2026-07-15_00-00-00-scheduled/\n"
    )
    assert rclone.listing("gdrive:Pal") == [
        "2026-07-15_06-00-00-scheduled",
        "2026-07-15_00-00-00-scheduled",
    ]


def test_prune_never_deletes_non_backup_dirs(monkeypatch):
    # The core data-safety guarantee: even with the user's own folders sorting
    # both above and below the backups, prune only ever purges palctl backups
    # beyond the retain count, never anything else.
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = (
        "Photos/\n"                          # sorts above (kept, untouched)
        "2026-07-15_00-00-00-scheduled/\n"
        "2026-07-14_00-00-00-scheduled/\n"
        "2026-07-13_00-00-00-scheduled/\n"
        "0-user-folder/\n"                   # sorts below (must NOT be purged)
    )
    doomed = rclone.prune("gdrive:Pal", retain=1)

    assert doomed == [
        "2026-07-14_00-00-00-scheduled",
        "2026-07-13_00-00-00-scheduled",
    ]
    purged = {c[1] for c in fake.calls if c[0] == "purge"}
    assert not any("Photos" in p or "user-folder" in p for p in purged)


def test_mirror_refuses_bare_remote_root(tmp_path: Path, monkeypatch):
    # Uploading dated folders straight into the drive root is refused: palctl
    # must live in a dedicated folder so retention can't reach the user's data.
    fake = FakeRclone().install(monkeypatch)
    with pytest.raises(RuntimeError, match="dedicated"):
        rclone.mirror(tmp_path / "b", "gdrive:")
    assert fake.calls == []  # nothing was uploaded


def test_prune_refuses_bare_remote_root(monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    fake.lsf_output = "2026-07-15_00-00-00-scheduled/\n"
    with pytest.raises(RuntimeError, match="dedicated"):
        rclone.prune("gdrive:", retain=1)
    assert fake.calls == []  # nothing listed, nothing purged


def test_test_remote_rejects_bare_root(monkeypatch):
    fake = FakeRclone().install(monkeypatch)
    ok, msg = rclone.test_remote("gdrive:")

    assert ok is False
    assert "dedicated" in msg
    assert fake.calls == []  # didn't even reach the network


def test_run_raises_a_clean_error_on_timeout(monkeypatch):
    # A stalled remote must surface as an actionable RuntimeError, not hang the
    # worker thread (or the GUI Test button) forever.
    fake = FakeRclone().install(monkeypatch)
    fake.timeout_on = "lsd"
    ok, msg = rclone.test_remote("gdrive:PalworldBackups")

    assert ok is False
    assert "timed out" in msg


def test_metadata_ops_pass_a_timeout(monkeypatch):
    # list/purge/test/version are bounded; only the upload (copy) may run long.
    calls_with_timeout: dict[str, object] = {}

    def record(argv, capture_output, text, timeout=None):
        import subprocess
        calls_with_timeout[argv[1]] = timeout
        out = "2026-07-15_00-00-00-scheduled/\n" if argv[1] == "lsf" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(rclone.shutil, "which", lambda _n: "/usr/bin/rclone")
    monkeypatch.setattr(rclone.subprocess, "run", record)

    rclone.listing("gdrive:Pal")
    rclone.test_remote("gdrive:Pal")
    rclone.check()
    rclone.mirror(Path("/tmp/b"), "gdrive:Pal")

    assert calls_with_timeout["lsf"] == rclone._META_TIMEOUT
    assert calls_with_timeout["lsd"] == rclone._META_TIMEOUT
    assert calls_with_timeout["version"] == rclone._META_TIMEOUT
    assert calls_with_timeout["copy"] is None  # a multi-GB upload isn't bounded
