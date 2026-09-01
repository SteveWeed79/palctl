"""Weighing a Level.sav, and — more importantly — failing to.

Two halves are tested separately on purpose. `count_records` is palctl's own
and decides whose records a future cleanup would remove, so it is exercised
directly against the shapes the parser produces, malformed ones included.
`scan()` is the safety boundary: it runs a third-party parser on a
multi-gigabyte file in a child process, and *every* way that can go wrong has
to come back as a report, never as an exception in the daemon.

What is NOT covered here: a successful end-to-end parse of a real `Level.sav`.
That needs a genuine multi-hundred-megabyte save — upstream's own fixtures are
56 MB and are not worth vendoring — so the parse path is exercised only for its
failure modes. The counting it feeds is covered exhaustively below.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from palctl import savescan
from palctl.savescan import ScanResult, count_records, scan

ZOE = "AAAA1111-0000-0000-0000-000000000000"
ANA = "BBBB2222-0000-0000-0000-000000000000"
NOBODY = "00000000-0000-0000-0000-000000000000"


def character(owner: str | None = None, key: str | None = None) -> dict:
    """One CharacterSaveParameterMap entry, shaped as the parser emits it."""
    params: dict = {}
    if owner is not None:
        params["OwnerPlayerUId"] = {"value": owner}
    return {
        "key": {"PlayerUId": {"value": key or NOBODY}},
        "value": {"RawData": {"value": {"object": {"SaveParameter": {"value": params}}}}},
    }


def world(characters: list | None = None, guilds: int = 0) -> dict:
    return {
        "CharacterSaveParameterMap": {"value": characters or []},
        "GroupSaveDataMap": {"value": [{} for _ in range(guilds)]},
    }


# ---------------- attribution ----------------


def test_pals_are_counted_against_whoever_caught_them():
    """The number that makes one departed player weigh more than another."""
    r = count_records(world([character(ZOE), character(ZOE), character(ANA)]))

    assert r.by_player == {
        "AAAA1111000000000000000000000000": 2,
        "BBBB2222000000000000000000000000": 1,
    }
    assert r.characters == 3
    assert r.attributed == 3


def test_a_players_own_character_is_attributed_by_its_key():
    """A player's own record carries no OwnerPlayerUId — the map key is the
    only thing that says whose it is."""
    r = count_records(world([character(owner=None, key=ZOE)]))

    assert r.by_player == {"AAAA1111000000000000000000000000": 1}
    assert r.unowned == 0


def test_wild_pals_are_never_attributed_to_anyone():
    """The all-zero GUID is Palworld's "nobody". Counting it as a player would
    invent an enormous fake account for a cleanup to target."""
    r = count_records(world([character(NOBODY), character(owner=None, key=NOBODY)]))

    assert r.by_player == {}
    assert r.unowned == 2


def test_guids_are_normalised_so_the_same_player_is_one_entry():
    """The save, the REST API and the filenames on disk each format GUIDs
    differently; comparing them unnormalised attributes nothing."""
    r = count_records(
        world([character(ZOE), character(ZOE.replace("-", "").lower())])
    )

    assert r.by_player == {"AAAA1111000000000000000000000000": 2}


def test_guilds_are_counted():
    assert count_records(world(guilds=4)).guilds == 4


def test_an_empty_world_is_a_valid_answer():
    r = count_records({})

    assert r.ok
    assert (r.characters, r.guilds, r.unowned) == (0, 0, 0)


# ---------------- shapes the parser may hand back ----------------


@pytest.mark.parametrize(
    "entry",
    [
        {},                                        # nothing at all
        {"value": None},                           # a null where a dict was
        {"value": {"RawData": []}},                # a list where a dict was
        {"value": {"RawData": {"value": "text"}}},  # a scalar mid-path
        "not-a-dict",                              # not even an entry
    ],
)
def test_a_malformed_entry_is_unowned_rather_than_a_crash(entry):
    """The save's shape moves between game patches. A chain of .get() calls
    raises the moment one level is a list — inside the child process, where the
    operator would see "couldn't read Level.sav" for a save that is fine."""
    r = count_records(world([entry]))

    assert r.ok
    assert r.unowned == 1


def test_a_missing_character_map_is_not_an_error():
    assert count_records({"GroupSaveDataMap": {"value": []}}).characters == 0


def test_a_null_character_map_is_not_an_error():
    assert count_records({"CharacterSaveParameterMap": {"value": None}}).characters == 0


# ---------------- the subprocess boundary ----------------


def test_a_missing_save_is_reported_not_raised(tmp_path):
    r = scan(tmp_path / "Level.sav")

    assert not r.ok
    assert "No Level.sav" in r.error


def test_a_timeout_comes_back_as_a_report(tmp_path, monkeypatch):
    level = tmp_path / "Level.sav"
    level.write_bytes(b"x")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=900)

    monkeypatch.setattr(subprocess, "run", slow)

    r = scan(level)
    assert not r.ok
    assert "longer than" in r.error


def test_an_oom_kill_is_explained_in_those_words(tmp_path, monkeypatch):
    """A negative return code is a signal. Quoting the number at someone whose
    server is stalling helps nobody; "ran out of memory" is the answer."""
    level = tmp_path / "Level.sav"
    level.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], -9, stdout="", stderr=""),
    )

    r = scan(level)
    assert not r.ok
    assert "ran out of memory" in r.error.lower()


def test_garbage_on_stdout_is_a_report_not_a_json_error(tmp_path, monkeypatch):
    level = tmp_path / "Level.sav"
    level.write_bytes(b"x")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
    )

    r = scan(level)
    assert not r.ok
    assert "unreadable output" in r.error


def test_the_child_reporting_failure_is_passed_through(tmp_path, monkeypatch):
    level = tmp_path / "Level.sav"
    level.write_bytes(b"x")
    payload = json.dumps({"ok": False, "error": "Level.sav is 9.0 GB"})
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout=payload, stderr=""),
    )

    r = scan(level)
    assert not r.ok
    assert "9.0 GB" in r.error


def test_a_successful_child_is_decoded(tmp_path, monkeypatch):
    level = tmp_path / "Level.sav"
    level.write_bytes(b"x")
    payload = json.dumps(
        {"ok": True, "characters": 900, "guilds": 12,
         "by_player": {"AAAA": 400}, "unowned": 500}
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=payload, stderr=""),
    )

    r = scan(level)
    assert r.ok
    assert (r.characters, r.guilds, r.unowned) == (900, 12, 500)
    assert r.by_player == {"AAAA": 400}


def test_a_save_too_big_to_parse_is_refused_with_a_reason(tmp_path, monkeypatch):
    """Refusing up front is information; being OOM-killed halfway is a mystery."""
    level = tmp_path / "Level.sav"
    level.write_bytes(b"x")
    monkeypatch.setattr(savescan, "MAX_LEVEL_BYTES", 0)

    r = savescan.analyse(level)
    assert not r.ok
    assert "past the" in r.error


# ---------------- the real child process ----------------


def test_the_child_runs_and_rejects_a_file_that_is_not_a_save(tmp_path):
    """End-to-end through the real subprocess and the real vendored parser:
    `python -m palctl.savescan` starts, imports the vendored library, fails to
    decompress nonsense, and reports it as JSON rather than a traceback."""
    level = tmp_path / "Level.sav"
    level.write_bytes(b"definitely not a palworld save" * 100)

    r = scan(level, timeout=120)

    assert not r.ok
    assert r.error  # something to show, whatever the parser objected to


def test_the_vendored_parser_is_importable():
    """Guards the vendoring itself: a re-vendor that drops a file, or a stale
    __pycache__, fails here rather than in front of someone with a 4 GB world."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'palctl/vendor');"
         " import palworld_save_tools.palsav, palworld_save_tools.gvas,"
         " palworld_save_tools.paltypes; print('ok')"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent,
    )

    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_the_vendored_license_travels_with_the_code():
    """MIT requires the notice to ship with the copy."""
    licence = Path(__file__).parent.parent / "palctl/vendor/palworld_save_tools/LICENSE"

    assert licence.is_file()
    assert "MIT License" in licence.read_text(encoding="utf-8")


def test_scan_result_is_falsy_about_nothing_it_did_not_measure():
    """A failed scan must not look like a world with zero players — the caller
    falls back to file sizes, and a confident 0 would suppress that."""
    r = ScanResult(ok=False, error="nope")

    assert not r.ok
    assert r.by_player == {}
    assert r.attributed == 0


# ---------------- the frozen build ----------------


def test_a_source_build_runs_the_child_with_dash_m():
    cmd = savescan.child_command(Path("/x/Level.sav"))

    assert cmd[1:3] == ["-m", "palctl.savescan"]
    assert cmd[3] == "/x/Level.sav"


def test_a_frozen_build_uses_a_marker_argument_instead(monkeypatch):
    """`sys.executable -m` is wrong when frozen: sys.executable is
    palctl-daemon.exe, which has no -m, so it would start a second daemon
    instead of a save reader. Same shape as the health-task bug."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    cmd = savescan.child_command(Path("/x/Level.sav"))

    assert cmd[1] == savescan.SAVESCAN_FLAG
    assert "-m" not in cmd


def test_the_frozen_entry_point_claims_only_its_own_argv():
    """Every frozen exe calls this first; it must not swallow a normal start."""
    assert savescan.frozen_entry([]) is None
    assert savescan.frozen_entry(["install-service"]) is None
    assert savescan.frozen_entry([savescan.SAVESCAN_FLAG]) is None  # no path


def test_the_frozen_entry_point_runs_a_save_read(tmp_path, capsys):
    level = tmp_path / "Level.sav"
    level.write_bytes(b"not a save")

    code = savescan.frozen_entry([savescan.SAVESCAN_FLAG, str(level)])

    assert code == 1  # it ran, and reported that it could not read the file
    assert json.loads(capsys.readouterr().out)["ok"] is False
