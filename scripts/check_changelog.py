"""Fail a release whose CHANGELOG heading doesn't match its tag.

`docs/VERSIONING.md` states the rule plainly — the git tag *is* the version
(setuptools-scm reads it; nothing is hand-written in code), so a release's
CHANGELOG heading must equal its tag exactly. It has drifted anyway: 1.2.5.6
shipped while the branch's section said 1.2.5.7, which leaves the shipped
binary's `--version` disagreeing with the notes describing it, and nothing in
CI noticed.

A rule nobody checks is a suggestion. Run from release.yml before anything is
built:

    python scripts/check_changelog.py v1.2.5.7

Also checks that the release section isn't empty and that a fresh
`## [Unreleased]` sits above it, since "shipped work left under Unreleased" is
the other half of the same rule.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)


def normalize(tag: str) -> str:
    """`v1.2.5.7` and `1.2.5.7` are the same release. Only the leading `v` is
    optional — anything else is a genuine mismatch and must fail."""
    return tag.strip().lstrip("vV")


def sections(text: str) -> list[tuple[str, str]]:
    """(version, body) for every `## [x]` heading, in file order."""
    out: list[tuple[str, str]] = []
    matches = list(HEADING.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # From the end of the heading *line*, not the end of the `[x]` match:
        # the rest of that line is the release date, and counting it as content
        # would make an otherwise empty section look described.
        line_end = text.find("\n", m.end())
        body_start = end if line_end == -1 else min(line_end + 1, end)
        out.append((m.group("version"), text[body_start:end]))
    return out


def check(text: str, tag: str) -> list[str]:
    """Every problem, not just the first — a release run should tell you
    everything to fix in one go."""
    version = normalize(tag)
    found = sections(text)
    problems: list[str] = []

    versions = [v for v, _ in found]
    if version not in versions:
        problems.append(
            f"CHANGELOG.md has no '## [{version}]' heading for tag {tag!r}. "
            f"Headings present: {', '.join(versions[:5]) or '(none)'}. "
            "docs/VERSIONING.md: the heading must equal the tag exactly."
        )
        return problems  # everything below needs the section to exist

    body = dict(found)[version]
    if not body.strip():
        problems.append(f"the '## [{version}]' section is empty — describe the release.")

    released = [v for v in versions if v.lower() != "unreleased"]
    if released and released[0] != version:
        problems.append(
            f"'## [{version}]' is not the newest release section "
            f"('{released[0]}' is above it). The release being tagged must be "
            "the top dated section."
        )

    if versions and versions[0].lower() != "unreleased":
        problems.append(
            "there is no '## [Unreleased]' section at the top. Tagging a release "
            "means opening a fresh empty one above it."
        )
    elif versions[0].lower() == "unreleased":
        unreleased = dict(found)["Unreleased"] if "Unreleased" in dict(found) else ""
        if unreleased.strip():
            problems.append(
                "'## [Unreleased]' still has entries. Shipped work must not sit "
                "under Unreleased — move it into the release section."
            )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0] if argv else 'check_changelog.py'} <tag>", file=sys.stderr)
        return 2
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    problems = check(path.read_text(encoding="utf-8"), argv[1])
    if problems:
        print(f"CHANGELOG.md does not match tag {argv[1]!r}:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"CHANGELOG.md matches tag {argv[1]!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
