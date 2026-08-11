"""Fail when the three copies of palctl's dependency list disagree.

The runtime dependencies are written down three times: `[project].dependencies`
in pyproject.toml (what `pip install palctl` gives an end user),
requirements.txt (a convenience copy), and a hand-typed
`pip install httpx psutil ...` in four CI jobs. Nothing compared them, so they
could drift — and the drift that matters is silent in the dangerous direction:
a package CI installs but pyproject doesn't declare makes every test pass
against an environment no user will ever have.

Installing from pyproject in those jobs would be the obvious fix and isn't
available: `pip install -e .` pulls PySide6 (a ~150MB Qt wheel) into six matrix
jobs that skip the GUI on purpose, and moving PySide6 to an extra changes what
`pip install palctl` gives an end user, who wants the GUI. So the lists stay
duplicated, and this makes the duplication checkable instead of hopeful.

    python scripts/check_deps.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Packages a CI job installs as tooling rather than as a palctl dependency.
# Listing them explicitly is the point: anything on a `pip install` line that
# is neither a declared dependency nor named here is a package CI relies on and
# the manifest doesn't, which is the drift this script exists to catch.
TOOLING = {
    "pytest",
    "pytest-cov",
    "ruff",
    "mypy",
    "pyinstaller",
    # discord.py imports the stdlib `audioop`, removed in 3.13; this backport
    # puts it back so the bot module still imports on that version.
    "audioop-lts",
}

# Dependencies a CI job may legitimately leave out.
ALLOWED_OMISSIONS = {
    # The Qt wheel. The unit matrix, the scenarios job and both Windows
    # lifecycle jobs never import the GUI, and downloading it six times over
    # would cost more than it proves. import-smoke installs the real package
    # (`pip install -e .`) and does exercise it.
    "pyside6",
}


def normalize(name: str) -> str:
    """PEP 503 name normalisation, so `discord.py` and `discord-py` are one
    package and casing never matters."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(spec: str) -> tuple[str, str]:
    """('httpx>=0.27') -> ('httpx', '>=0.27'). Comments and blanks are callers'
    business; this assumes a bare requirement."""
    m = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", spec.strip())
    if not m:
        return normalize(spec), ""
    return normalize(m.group(1)), m.group(2).replace(" ", "")


def declared() -> dict[str, str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(parse_requirement(d) for d in data["project"]["dependencies"])


def from_requirements_txt() -> dict[str, str]:
    out = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            name, spec = parse_requirement(line)
            out[name] = spec
    return out


def pip_install_lines(workflow: str) -> list[tuple[int, list[str]]]:
    """Every `pip install ...` in a workflow, as (line number, package names).

    Read with a regex rather than a YAML parser on purpose: this runs in the
    lint job, which installs ruff and nothing else, and a checker that needs its
    own dependency installed is one more thing that can be quietly skipped.
    """
    out = []
    for i, line in enumerate(workflow.splitlines(), start=1):
        m = re.search(r"pip install\s+(.+)$", line)
        if not m:
            continue
        args = m.group(1).strip()
        if args.startswith("-") or " -e " in f" {args} ":
            continue  # `pip install -e .` installs the manifest itself
        out.append((i, [a for a in args.split() if not a.startswith("-")]))
    return out


def check() -> list[str]:
    problems: list[str] = []
    deps = declared()

    reqs = from_requirements_txt()
    if reqs != deps:
        for name in sorted(set(deps) | set(reqs)):
            if name not in reqs:
                problems.append(f"requirements.txt is missing {name}{deps[name]}")
            elif name not in deps:
                problems.append(
                    f"requirements.txt has {name}, which pyproject.toml does not declare"
                )
            elif reqs[name] != deps[name]:
                problems.append(
                    f"{name}: pyproject says '{deps[name]}', requirements.txt says "
                    f"'{reqs[name]}'"
                )

    required = {n for n in deps if n not in ALLOWED_OMISSIONS}
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        for lineno, packages in pip_install_lines(text):
            names = {parse_requirement(p)[0] for p in packages}
            runtime = names & set(deps)
            unknown = names - set(deps) - TOOLING
            if unknown:
                problems.append(
                    f"{wf.name}:{lineno} installs {', '.join(sorted(unknown))}, which is "
                    "neither declared in pyproject.toml nor listed as tooling in "
                    "scripts/check_deps.py"
                )
            if not runtime:
                continue  # a tooling-only line
            missing = required - names
            if missing:
                problems.append(
                    f"{wf.name}:{lineno} installs palctl's dependencies but omits "
                    f"{', '.join(sorted(missing))} — the job is testing against an "
                    "environment no user will have"
                )
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("dependency lists disagree:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nFix pyproject.toml, requirements.txt and the pip install lines in "
            ".github/workflows/ together."
        )
        return 1
    print(f"OK: {len(declared())} declared dependencies, consistent everywhere")
    return 0


if __name__ == "__main__":
    sys.exit(main())
