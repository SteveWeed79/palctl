"""The release gate for `docs/VERSIONING.md`'s one rule.

The rule ("the CHANGELOG heading must equal the tag, exactly") is older than the
drift that broke it: 1.2.5.6 shipped while the changelog section said 1.2.5.7.
It broke because nothing checked, so what's pinned here is the checking.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_changelog import check, normalize, sections

GOOD = """# Changelog

## [Unreleased]

## [1.2.5.7] — 2026-08-10

### Fixed
- something real

## [1.2.5.6] — 2026-08-01

### Fixed
- something older
"""


def test_a_matching_tag_passes_with_or_without_the_v():
    assert check(GOOD, "1.2.5.7") == []
    assert check(GOOD, "v1.2.5.7") == []


def test_the_exact_drift_that_shipped_is_caught():
    """Tagged 1.2.5.6 while the top section said 1.2.5.7."""
    problems = check(GOOD, "v1.2.5.6")
    assert problems
    assert "not the newest release section" in " ".join(problems)


def test_a_tag_with_no_section_at_all_is_caught():
    problems = check(GOOD, "v9.9.9.9")
    assert len(problems) == 1
    assert "no '## [9.9.9.9]' heading" in problems[0]


def test_shipped_work_left_under_unreleased_is_caught():
    text = GOOD.replace("## [Unreleased]\n", "## [Unreleased]\n\n- forgot to move this\n")
    problems = check(text, "1.2.5.7")
    assert any("still has entries" in p for p in problems)


def test_a_missing_unreleased_section_is_caught():
    text = GOOD.replace("## [Unreleased]\n\n", "")
    problems = check(text, "1.2.5.7")
    assert any("no '## [Unreleased]' section" in p for p in problems)


def test_an_empty_release_section_is_caught():
    text = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0.0] — 2026-01-01\n"
    problems = check(text, "1.0.0.0")
    assert any("is empty" in p for p in problems)


def test_only_a_leading_v_is_optional():
    """`release-1.2.5.7` is not the same tag, and quietly accepting it would
    reintroduce exactly the drift this guards."""
    assert normalize("v1.2.5.7") == "1.2.5.7"
    assert normalize("1.2.5.7") == "1.2.5.7"
    assert check(GOOD, "release-1.2.5.7") != []


def test_sections_are_read_in_file_order():
    assert [v for v, _ in sections(GOOD)] == ["Unreleased", "1.2.5.7", "1.2.5.6"]


def test_the_real_changelog_parses():
    """A smoke check against the actual file: if this stops finding headings,
    the gate would pass everything."""
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    found = sections(text)
    assert len(found) > 5
    assert found[0][0] == "Unreleased"
