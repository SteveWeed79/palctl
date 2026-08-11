"""The gate on the three copies of palctl's dependency list.

The lists can't be collapsed into one (see scripts/check_deps.py for why), so
the duplication is deliberate — which makes an automated comparison the only
thing standing between it and drift. The direction that matters is silent: a
package CI installs but pyproject doesn't declare makes every test pass against
an environment no user will ever have.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_deps import check, normalize, parse_requirement, pip_install_lines


def test_the_repository_itself_is_consistent():
    """Not a unit test — the gate, run against the real files. If this fails,
    the three lists have actually diverged."""
    assert check() == []


# ---------------- name handling ----------------


def test_names_are_normalised_the_way_pip_normalises_them():
    # `discord.py` on the pip line and `discord.py` in pyproject are one
    # package; so are Pillow/pillow and a-b/a_b. PEP 503.
    assert normalize("discord.py") == "discord-py"
    assert normalize("PySide6") == "pyside6"
    assert normalize("a_b.c") == "a-b-c"


def test_a_requirement_splits_into_name_and_specifier():
    assert parse_requirement("httpx>=0.27") == ("httpx", ">=0.27")
    assert parse_requirement("ruff == 0.16.2") == ("ruff", "==0.16.2")
    assert parse_requirement("psutil") == ("psutil", "")


# ---------------- reading the workflows ----------------


def test_pip_install_lines_are_found_and_editable_installs_skipped():
    wf = """
      - run: pip install httpx psutil
      - run: pip install -e .
      - run: pip install ruff==0.16.2
      - run: pytest -v
    """
    found = pip_install_lines(wf)
    assert [pkgs for _, pkgs in found] == [["httpx", "psutil"], ["ruff==0.16.2"]]
    # `pip install -e .` installs the manifest, so it can't disagree with it.


def test_line_numbers_are_reported_so_a_failure_is_findable():
    wf = "a\nb\n      - run: pip install httpx\n"
    assert pip_install_lines(wf)[0][0] == 3


# ---------------- what it catches ----------------


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A miniature repo, so the failure modes can be provoked without touching
    the real manifests."""
    import check_deps

    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    monkeypatch.setattr(check_deps, "ROOT", tmp_path)

    def write(pyproject_deps, requirements, workflow):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\ndependencies = [\n"
            + "".join(f'  "{d}",\n' for d in pyproject_deps)
            + "]\n"
        )
        (tmp_path / "requirements.txt").write_text("\n".join(requirements) + "\n")
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text(workflow)

    return write


def test_a_ci_only_package_is_caught(fake_repo):
    """The dangerous direction: CI installs it, users never get it."""
    fake_repo(
        ["httpx>=0.27"],
        ["httpx>=0.27"],
        "      - run: pip install httpx tenacity\n",
    )
    problems = check()
    assert any("tenacity" in p and "neither declared" in p for p in problems), problems


def test_a_dependency_ci_forgets_to_install_is_caught(fake_repo):
    fake_repo(
        ["httpx>=0.27", "psutil>=6.0"],
        ["httpx>=0.27", "psutil>=6.0"],
        "      - run: pip install httpx\n",
    )
    problems = check()
    assert any("omits psutil" in p for p in problems), problems


def test_a_specifier_that_drifted_is_caught(fake_repo):
    fake_repo(
        ["httpx>=0.27"],
        ["httpx>=0.28"],
        "      - run: pip install httpx\n",
    )
    problems = check()
    assert any("pyproject says '>=0.27'" in p for p in problems), problems


def test_a_requirement_only_in_requirements_txt_is_caught(fake_repo):
    fake_repo(["httpx>=0.27"], ["httpx>=0.27", "rich>=13"], "      - run: pip install httpx\n")
    assert any("rich" in p for p in check())


def test_pyside6_may_be_left_out_of_a_job_that_skips_the_gui(fake_repo):
    """The one allowed omission, and the reason the lists can't just be
    `pip install -e .`: six matrix jobs would each pull a ~150MB Qt wheel to
    run tests that never import it."""
    fake_repo(
        ["httpx>=0.27", "PySide6>=6.7"],
        ["httpx>=0.27", "PySide6>=6.7"],
        "      - run: pip install httpx\n",
    )
    assert check() == []


def test_tooling_on_a_pip_line_is_not_mistaken_for_a_dependency(fake_repo):
    fake_repo(
        ["httpx>=0.27"],
        ["httpx>=0.27"],
        "      - run: pip install httpx pytest\n      - run: pip install ruff==0.16.2\n",
    )
    assert check() == []
