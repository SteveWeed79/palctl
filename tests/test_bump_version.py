"""What the release-cut automation must never get wrong.

The whole point of `scripts/bump_version.py` is that nobody hand-edits the
changelog and hand-picks the tag any more. That only helps if the machine is
right about the number, so the arithmetic in docs/VERSIONING.md's table, the
choice of base, and the refusal to reuse a version are all pinned here.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bump_version import (  # noqa: E402
    bump,
    cut,
    highest,
    main,
    parse,
    resolve_base,
    unreleased_body,
)
from check_changelog import check  # noqa: E402

BEFORE = """# Changelog

## [Unreleased]

### Fixed
- a real fix that is about to ship

## [1.2.8.0] — 2026-09-01

### Added
- the previous release
"""


def test_the_table_in_versioning_md():
    """Bump one part, reset everything to its right — the worked examples."""
    assert bump("1.2.5.5", "feature") == "1.2.6.0"
    assert bump("1.2.6.0", "patch") == "1.2.6.1"
    assert bump("1.2.6.1", "major") == "2.0.0.0"
    assert bump("1.2.6.1", "minor") == "1.3.0.0"


def test_a_short_tag_is_zero_extended():
    """`1.2.8` is `1.2.8.0`, so the next patch after it is `1.2.8.1` — not
    `1.2.9`, and not a crash."""
    assert parse("1.2.8") == (1, 2, 8, 0)
    assert parse("v1.2.8") == (1, 2, 8, 0)
    assert bump("1.2.8", "patch") == "1.2.8.1"


def test_a_nonsense_base_is_refused():
    for bad in ("1.2.8-rc1", "nightly", "1.2.3.4.5", ""):
        with pytest.raises(ValueError):
            parse(bad)


def test_the_base_is_the_highest_tag_not_the_newest_one_made():
    """Tags come back in whatever order git lists them, and `1.2.10.0` sorts
    below `1.2.9.0` as text. The number decides, not the string."""
    assert highest(["1.2.9.0", "1.2.10.0", "v1.2.8"]) == "1.2.10.0"
    assert highest(["1.2.8-rc1", "nightly"]) is None


def test_prereleases_are_not_a_base():
    assert highest(["1.2.8.0", "1.3.0.0-rc1"]) == "1.2.8.0"


def test_the_changelog_counts_too_when_a_release_is_cut_but_not_tagged():
    """If the cut commit landed and the tag push failed, the next run must not
    hand out the same number again."""
    assert resolve_base(BEFORE, []) == "1.2.8.0"
    assert resolve_base(BEFORE, ["1.2.7.0"]) == "1.2.8.0"
    assert resolve_base(BEFORE, ["1.2.9.0"]) == "1.2.9.0"
    assert resolve_base("# Changelog\n\n## [Unreleased]\n", []) == "0.0.0.0"


def test_cutting_renames_unreleased_and_opens_a_fresh_one():
    after = cut(BEFORE, "1.2.8.1", date(2026, 9, 2))
    assert "## [Unreleased]\n\n## [1.2.8.1] — 2026-09-02\n" in after
    # The entries move into the release; nothing is dropped and nothing older
    # is touched.
    assert "- a real fix that is about to ship" in after
    assert "## [1.2.8.0] — 2026-09-01" in after
    assert not unreleased_body(after).strip()


def test_a_cut_release_passes_the_release_gate():
    """The two scripts have to agree, or the automation cheerfully produces a
    tag that `release.yml` then refuses to build."""
    after = cut(BEFORE, "1.2.8.1", date(2026, 9, 2))
    assert check(after, "1.2.8.1") == []
    assert check(after, "v1.2.8.1") == []


def test_nothing_under_unreleased_is_not_a_release(tmp_path, capsys):
    """A merge that ships nothing must exit 3 (skip), not fail the run."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.0.0.0] — 2026-01-01\n\n- old\n",
        encoding="utf-8",
    )
    assert main(["bump_version.py", "--changelog", str(path), "--part", "patch"]) == 3
    assert "Nothing under" in capsys.readouterr().err
    # And nothing was written.
    assert "1.0.0.1" not in path.read_text(encoding="utf-8")


def test_apply_writes_the_file_and_prints_the_version(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(BEFORE, encoding="utf-8")
    rc = main(
        [
            "bump_version.py",
            "--changelog",
            str(path),
            "--part",
            "patch",
            "--base",
            "1.2.8.0",
            "--date",
            "2026-09-02",
            "--apply",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "1.2.8.1"
    assert "## [1.2.8.1] — 2026-09-02" in path.read_text(encoding="utf-8")


def test_without_apply_nothing_is_written(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(BEFORE, encoding="utf-8")
    assert main(["bump_version.py", "--changelog", str(path), "--base", "1.2.8.0"]) == 0
    assert capsys.readouterr().out.strip() == "1.2.8.1"
    assert path.read_text(encoding="utf-8") == BEFORE


def test_a_version_that_already_exists_is_refused(tmp_path, capsys):
    """Reusing a number would either move a shipped tag or leave two sections
    claiming the same release."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(BEFORE.replace("## [1.2.8.0]", "## [1.2.8.1]"), encoding="utf-8")
    rc = main(["bump_version.py", "--changelog", str(path), "--base", "1.2.8.0", "--apply"])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    assert "## [Unreleased]\n\n### Fixed" in path.read_text(encoding="utf-8")
