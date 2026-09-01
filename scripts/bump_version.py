"""Work out the next version and move `[Unreleased]` into it.

`docs/VERSIONING.md` describes a ritual for every PR: read the diff, pick the
part to bump, rename `## [Unreleased]` to that number with today's date, open a
fresh empty `## [Unreleased]` above it, then push a tag spelled the same way.
Every step is mechanical, and every step has been got wrong by hand — the whole
1.2 line once sat under `[Unreleased]` with no headings at all, 1.2.5.6 shipped
against notes titled 1.2.5.7, and the 1.2.8 release failed twice on a tag that
didn't match its heading. `scripts/check_changelog.py` catches the mistakes at
release time; this stops making them.

    python scripts/bump_version.py --part patch             # print the number
    python scripts/bump_version.py --part patch --apply     # and rewrite CHANGELOG.md

`.github/workflows/release-cut.yml` runs it, commits, tags, and hands the tag to
the Release workflow.

The base is the highest existing **git tag**, not the changelog: the tag is the
version (setuptools-scm reads it), and the changelog is the side that drifts.

Exit codes: 0 cut, 2 usage, 3 nothing to release (a bare `[Unreleased]`, which
is a normal state and not a failure), 1 anything genuinely wrong.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date as date_cls
from pathlib import Path

from check_changelog import check, key, normalize, sections

REPO_ROOT = Path(__file__).resolve().parent.parent

# The four parts of docs/VERSIONING.md's MAJOR.MINOR.FEATURE.PATCH, in order,
# so a bump is "raise this index, zero everything to its right".
PARTS = ("major", "minor", "feature", "patch")

NOTHING_TO_RELEASE = 3

# `## [Unreleased]` and the rest of its line (a date someone typed there, say),
# matched so the whole heading line can be replaced.
UNRELEASED_HEADING = re.compile(r"^## \[Unreleased\][^\n]*\n", re.MULTILINE | re.IGNORECASE)


def parse(version: str) -> tuple[int, int, int, int]:
    """`1.2.8` → `(1, 2, 8, 0)`. Short spellings are zero-extended, since a
    trailing zero is routinely left off a tag and `1.2.8` names the same release
    as `1.2.8.0`."""
    parts = normalize(version).split(".")
    if not all(re.fullmatch(r"[0-9]+", p) for p in parts) or not 1 <= len(parts) <= 4:
        raise ValueError(
            f"{version!r} is not a MAJOR.MINOR.FEATURE.PATCH version. "
            "docs/VERSIONING.md: four numeric parts, trailing zeros optional."
        )
    nums = [int(p) for p in parts] + [0] * (4 - len(parts))
    return nums[0], nums[1], nums[2], nums[3]


def bump(base: str, part: str) -> str:
    """The next version after `base` for a `part` bump: raise that part, reset
    everything to its right to 0 — the table in docs/VERSIONING.md."""
    if part not in PARTS:
        raise ValueError(f"unknown part {part!r}; expected one of {', '.join(PARTS)}")
    nums = list(parse(base))
    i = PARTS.index(part)
    nums[i] += 1
    for j in range(i + 1, len(nums)):
        nums[j] = 0
    return ".".join(str(n) for n in nums)


def git_tags(repo: Path) -> list[str]:
    """Every tag in the repo, or an empty list where git can't answer (a shallow
    CI checkout that fetched no tags). The caller decides what that means."""
    try:
        out = subprocess.run(
            ["git", "tag", "--list"],
            cwd=repo,
            capture_output=True,
            text=True,
            # Never the locale's encoding: this runs on the Windows CI too,
            # where that is cp1252 and a tag git hands back in UTF-8 would come
            # out mojibake — or, on the strict path, not at all.
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def highest(versions: list[str]) -> str | None:
    """The largest version in `versions`, ignoring anything that isn't a plain
    dotted-decimal release (`v1.2.8` counts, `1.2.8-rc1` and `nightly` don't:
    a prerelease is not the base for the next number)."""
    numeric = []
    for v in versions:
        try:
            numeric.append(parse(v))
        except ValueError:
            continue
    if not numeric:
        return None
    return ".".join(str(n) for n in max(numeric))


def released_headings(text: str) -> list[str]:
    """Dated release headings, newest first — everything but `[Unreleased]`."""
    return [v for v, _ in sections(text) if v.lower() != "unreleased"]


def unreleased_body(text: str) -> str:
    for version, body in sections(text):
        if version.lower() == "unreleased":
            return body
    return ""


def resolve_base(text: str, tags: list[str]) -> str:
    """The version being bumped *from*.

    The highest git tag, since the tag is the version. The changelog's newest
    dated heading is taken into account too, so a release cut but not yet tagged
    can't hand out a number that is already spoken for. With neither, this is
    the first release and everything starts from `0.0.0.0`.
    """
    candidates = [v for v in (highest(tags), highest(released_headings(text))) if v]
    return highest(candidates) or "0.0.0.0"


def cut(text: str, version: str, when: date_cls) -> str:
    """Rename `## [Unreleased]` to this release and open a fresh empty one above
    it, leaving the entries — and everything else in the file — untouched."""
    m = UNRELEASED_HEADING.search(text)
    if m is None:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' heading to cut a release from.")
    heading = f"## [Unreleased]\n\n## [{version}] — {when.isoformat()}\n"
    return text[: m.start()] + heading + text[m.end() :]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--part",
        choices=PARTS,
        default="patch",
        help="which part to bump (default: patch, the everyday PR)",
    )
    ap.add_argument("--base", help="version to bump from (default: the highest git tag)")
    ap.add_argument("--date", help="release date, YYYY-MM-DD (default: today)")
    ap.add_argument("--changelog", type=Path, default=REPO_ROOT / "CHANGELOG.md")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="rewrite CHANGELOG.md; without it nothing is written and the version is only printed",
    )
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="cut a release even with nothing under [Unreleased] (a re-release of unchanged code)",
    )
    args = ap.parse_args(argv[1:])

    text = args.changelog.read_text(encoding="utf-8")
    repo = args.changelog.resolve().parent
    tags = git_tags(repo)

    if not args.allow_empty and not unreleased_body(text).strip():
        print(
            "Nothing under '## [Unreleased]' — there is no release to cut. "
            "(Pass --allow-empty to cut one anyway.)",
            file=sys.stderr,
        )
        return NOTHING_TO_RELEASE

    base = args.base or resolve_base(text, tags)
    try:
        version = bump(base, args.part)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    # Refuse to hand out a number that already exists. Reusing one would either
    # move a shipped tag or leave two sections claiming the same release.
    taken = {key(normalize(v)) for v in tags + released_headings(text)}
    if key(version) in taken:
        print(
            f"{version} already exists (as a tag or a CHANGELOG heading) — "
            f"refusing to reuse it. Base was {base}.",
            file=sys.stderr,
        )
        return 1

    updated = cut(text, version, _when(args.date))

    # The gate that guards the release, run here against the text about to be
    # written: a cut that couldn't ship is a bug in this script, and finding it
    # now beats finding it in the release run.
    problems = check(updated, version)
    if problems:
        print(f"the cut release {version} would fail scripts/check_changelog.py:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    if args.apply:
        args.changelog.write_text(updated, encoding="utf-8")

    print(version)
    return 0


def _when(value: str | None) -> date_cls:
    if not value:
        return date_cls.today()
    return date_cls.fromisoformat(value)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
