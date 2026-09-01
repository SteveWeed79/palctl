"""The only code in palctl that rewrites a world.

Almost every test here asserts a refusal, because that is what the module is:
a series of gates with a mutation at the end. Each gate exists because skipping
it deletes something somebody wanted, and each is tested for the case that
would delete it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from palctl import backups, saveaudit, saveprune, savescan
from palctl.saveprune import (
    EXCLUSIONS_NAME,
    PruneTarget,
    exclusions_readable,
    format_plan,
    load_exclusions,
    plan_prune,
    record_exclusions,
    run_prune,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
ZOE = "AAAA1111000000000000000000000000"
ANA = "BBBB2222000000000000000000000000"


def iso(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def make_world(tmp_path: Path, guids: dict[str, int] | None = None) -> Path:
    world = tmp_path / "SaveGames" / "0" / "W1"
    (world / "Players").mkdir(parents=True)
    (world / "Level.sav").write_bytes(b"x" * 4096)
    for guid, size in (guids or {}).items():
        (world / "Players" / f"{guid}.sav").write_bytes(b"p" * size)
    return world


def audit_of(world: Path, seen: dict) -> saveaudit.SaveAudit:
    return saveaudit.audit(world, seen)


def scan_of(**by_player: int) -> savescan.ScanResult:
    return savescan.ScanResult(
        ok=True,
        characters=sum(by_player.values()) + 500,
        guilds=3,
        by_player=dict(by_player),
        unowned=500,
    )


# ---------------- who gets targeted ----------------


def test_an_inactive_player_with_records_is_a_target(tmp_path):
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(
        audit_of(world, {ZOE: ("zoe", iso(200))}), scan_of(**{ZOE: 412}), now=NOW
    )

    assert [t.name for t in plan.targets] == ["zoe"]
    assert plan.records == 412


def test_a_recent_player_is_never_a_target(tmp_path):
    world = make_world(tmp_path, {ANA: 100})
    plan = plan_prune(
        audit_of(world, {ANA: ("ana", iso(2))}), scan_of(**{ANA: 900}), now=NOW
    )

    assert plan.targets == []


def test_an_unidentifiable_save_is_never_a_target(tmp_path):
    """saveaudit's rule 1, carried into the thing that deletes: palctl does not
    remove records it cannot attribute."""
    world = make_world(tmp_path, {"MYSTERY": 100})
    plan = plan_prune(audit_of(world, {}), scan_of(MYSTERY=900), now=NOW)

    assert plan.targets == []
    assert plan.unknown == 1


def test_an_excluded_player_is_reported_and_spared(tmp_path):
    """A player pruned last month who came back is a new character to the game.
    Without the exclusion list there is no way to tell that from never left."""
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(
        audit_of(world, {ZOE: ("zoe", iso(200))}),
        scan_of(**{ZOE: 412}),
        exclusions={ZOE},
        now=NOW,
    )

    assert plan.targets == []
    assert plan.excluded == ["zoe"]


def test_an_idle_player_with_nothing_in_the_save_is_not_worth_a_rewrite(tmp_path):
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(audit_of(world, {ZOE: ("zoe", iso(200))}), scan_of(), now=NOW)

    assert plan.targets == []


def test_a_failed_scan_blocks_the_whole_plan(tmp_path):
    """palctl will not remove records it could not count first."""
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(
        audit_of(world, {ZOE: ("zoe", iso(200))}),
        savescan.ScanResult(ok=False, error="out of memory"),
        now=NOW,
    )

    assert not plan.safe
    assert "could not be read" in plan.blockers[0]


def test_a_plan_that_would_gut_the_world_is_refused(tmp_path):
    """Removing 90% of the records is far likelier to be an attribution bug
    than a world that is 90% abandoned."""
    world = make_world(tmp_path, {ZOE: 100})
    scan = savescan.ScanResult(ok=True, characters=1000, by_player={ZOE: 990})
    plan = plan_prune(audit_of(world, {ZOE: ("zoe", iso(200))}), scan, now=NOW)

    assert not plan.safe
    assert "too much of the world" in plan.blockers[0]


# ---------------- the exclusion list ----------------


def test_exclusions_round_trip(tmp_path):
    world = make_world(tmp_path)
    record_exclusions(world, [PruneTarget(ZOE, "zoe", 412, 200.0)])

    assert load_exclusions(world) == {ZOE}
    stored = json.loads((world / EXCLUSIONS_NAME).read_text())
    assert stored["players"][ZOE]["name"] == "zoe"
    assert stored["players"][ZOE]["records"] == 412


def test_recording_exclusions_keeps_the_previous_ones(tmp_path):
    world = make_world(tmp_path)
    record_exclusions(world, [PruneTarget(ZOE, "zoe", 1, 200.0)])
    record_exclusions(world, [PruneTarget(ANA, "ana", 2, 300.0)])

    assert load_exclusions(world) == {ZOE, ANA}


def test_a_missing_exclusion_file_is_fine(tmp_path):
    world = make_world(tmp_path)
    assert exclusions_readable(world)
    assert load_exclusions(world) == set()


def test_an_unreadable_exclusion_file_is_a_refusal_not_an_empty_list(tmp_path):
    """The dangerous case: an empty read would WIDEN the target set and
    re-prune a returning player."""
    world = make_world(tmp_path)
    (world / EXCLUSIONS_NAME).write_text("{ this is not json")

    assert not exclusions_readable(world)

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {},
        server_stopped=True, apply=True, now=NOW,
    )
    assert not outcome.ok
    assert "already protected" in outcome.error


# ---------------- run_prune's gates ----------------


def test_a_dry_run_is_the_default_and_changes_nothing(tmp_path, monkeypatch):
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(savescan, "scan", lambda *a, **k: scan_of(**{ZOE: 412}))
    took_backup = []
    monkeypatch.setattr(backups, "create", lambda *a, **k: took_backup.append(1))

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=True, now=NOW,
    )

    assert outcome.ok
    assert not outcome.applied
    assert took_backup == []  # not even a backup on a dry run
    assert outcome.plan.records == 412


def test_a_running_server_blocks_the_rewrite(tmp_path, monkeypatch):
    """Rewriting a save the server has open corrupts it, and its next autosave
    would overwrite the result anyway."""
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(savescan, "scan", lambda *a, **k: scan_of(**{ZOE: 412}))

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=False, apply=True, now=NOW,
    )

    assert not outcome.ok
    assert "still running" in outcome.error


def test_a_backup_that_does_not_verify_stops_everything(tmp_path, monkeypatch):
    """Rule 3: an undo nobody checked is not an undo."""
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(savescan, "scan", lambda *a, **k: scan_of(**{ZOE: 412}))
    monkeypatch.setattr(
        backups, "create",
        lambda *a, **k: backups.Backup("2026-01-01_00-00-00-pre-prune", tmp_path, 1.0, NOW),
    )
    monkeypatch.setattr(
        backups, "verify",
        lambda *a, **k: backups.VerifyReport("b", ok=False, problems=["Level.sav: truncated"]),
    )
    rewrote = []
    monkeypatch.setattr(savescan, "prune_records", lambda *a, **k: rewrote.append(1))

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=True, apply=True, now=NOW,
    )

    assert not outcome.ok
    assert "did not verify" in outcome.error
    assert rewrote == []  # the rewrite was never reached


def test_a_backup_failure_stops_everything(tmp_path, monkeypatch):
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(savescan, "scan", lambda *a, **k: scan_of(**{ZOE: 412}))

    def no_backup(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(backups, "create", no_backup)

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=True, apply=True, now=NOW,
    )

    assert not outcome.ok
    assert "disk full" in outcome.error


def test_a_successful_prune_records_the_exclusions(tmp_path, monkeypatch):
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(savescan, "scan", lambda *a, **k: scan_of(**{ZOE: 412}))
    monkeypatch.setattr(
        backups, "create",
        lambda *a, **k: backups.Backup("2026-01-01_00-00-00-pre-prune", tmp_path, 1.0, NOW),
    )
    monkeypatch.setattr(backups, "verify", lambda *a, **k: backups.VerifyReport("b", ok=True))
    monkeypatch.setattr(
        savescan, "prune_records",
        lambda *a, **k: {"ok": True, "removed": 412, "original": "Level.sav.pre-prune"},
    )

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=True, apply=True, now=NOW,
    )

    assert outcome.ok and outcome.applied
    assert load_exclusions(world) == {ZOE}  # so a return isn't pruned twice
    assert "412" in outcome.message


def test_a_failed_rewrite_leaves_no_exclusions_behind(tmp_path, monkeypatch):
    """If the rewrite didn't happen, the player was not pruned, and recording
    them would protect them from a prune that still needs to happen."""
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(savescan, "scan", lambda *a, **k: scan_of(**{ZOE: 412}))
    monkeypatch.setattr(
        backups, "create",
        lambda *a, **k: backups.Backup("2026-01-01_00-00-00-pre-prune", tmp_path, 1.0, NOW),
    )
    monkeypatch.setattr(backups, "verify", lambda *a, **k: backups.VerifyReport("b", ok=True))
    monkeypatch.setattr(
        savescan, "prune_records",
        lambda *a, **k: {"ok": False, "error": "the rewritten save did not read back"},
    )

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=True, apply=True, now=NOW,
    )

    assert not outcome.ok
    assert load_exclusions(world) == set()


def test_a_blocked_plan_never_reaches_the_backup(tmp_path, monkeypatch):
    world = make_world(tmp_path, {ZOE: 100})
    monkeypatch.setattr(
        savescan, "scan", lambda *a, **k: savescan.ScanResult(ok=False, error="nope")
    )
    took_backup = []
    monkeypatch.setattr(backups, "create", lambda *a, **k: took_backup.append(1))

    outcome = run_prune(
        world, tmp_path / "sg", tmp_path / "b", {ZOE: ("zoe", iso(200))},
        server_stopped=True, apply=True, now=NOW,
    )

    assert not outcome.ok
    assert took_backup == []


# ---------------- the report ----------------


def test_the_dry_run_report_says_it_changed_nothing():
    plan = saveprune.PrunePlan(targets=[PruneTarget(ZOE, "zoe", 412, 200.0)])
    text = format_plan(plan)

    assert "Would remove" in text
    assert "This was a dry run" in text


def test_the_applied_report_speaks_in_the_past_tense():
    plan = saveprune.PrunePlan(targets=[PruneTarget(ZOE, "zoe", 412, 200.0)])
    text = format_plan(plan, applied=True)

    assert "Removed 412" in text
    assert "dry run" not in text


def test_a_blocked_plan_leads_with_the_refusal():
    plan = saveprune.PrunePlan(blockers=["Level.sav could not be read"])
    assert format_plan(plan).startswith("Refusing to prune:")


def test_the_report_mentions_what_it_could_not_identify():
    plan = saveprune.PrunePlan(
        targets=[PruneTarget(ZOE, "zoe", 5, 200.0)], unknown=7
    )
    assert "7 save file(s) palctl can't identify" in format_plan(plan)


# ---------------- the child-side rewrite ----------------


def test_the_owner_rule_matches_the_counting_rule():
    """A record counted against a player must be the same record removed for
    them — the two rules being separate functions is the risk."""
    entry = {
        "key": {"PlayerUId": {"value": "00000000-0000-0000-0000-000000000000"}},
        "value": {"RawData": {"value": {"object": {"SaveParameter": {
            "value": {"OwnerPlayerUId": {"value": "aaaa1111-0000-0000-0000-000000000000"}}
        }}}}},
    }

    counted = savescan.count_records(
        {"CharacterSaveParameterMap": {"value": [entry]}}
    ).by_player

    assert savescan._owner_of(entry) in counted


def test_wild_pals_are_never_owned_by_anyone():
    entry = {"key": {"PlayerUId": {"value": "00000000-0000-0000-0000-000000000000"}},
             "value": {}}
    assert savescan._owner_of(entry) == ""


def test_prune_records_refuses_an_empty_target_list(tmp_path):
    assert savescan.prune_records(tmp_path / "Level.sav", [])["ok"] is False


def test_prune_records_reports_a_child_failure_rather_than_raising(tmp_path):
    """End-to-end through the real subprocess: a file that is not a save comes
    back as a report."""
    level = tmp_path / "Level.sav"
    level.write_bytes(b"not a save at all")

    result = savescan.prune_records(level, [ZOE], timeout=120)

    assert not result["ok"]
    assert result["error"]
    assert level.read_bytes() == b"not a save at all"  # untouched


@pytest.mark.parametrize("frozen", [False, True])
def test_the_prune_child_command_carries_its_targets(tmp_path, monkeypatch, frozen):
    """A prune carries a mode flag, a path and a target list. The frozen entry
    point forwards everything after its marker — dropping the tail would turn a
    prune into a malformed read."""
    if frozen:
        monkeypatch.setattr(savescan, "sys", savescan.sys)
        monkeypatch.setattr(savescan.sys, "frozen", True, raising=False)

    argv = savescan.child_command(tmp_path / "Level.sav")
    argv.insert(-1, savescan.PRUNE_FLAG)
    argv.append(f"{ZOE},{ANA}")

    child = argv[1:]
    if frozen:
        assert child[0] == savescan.SAVESCAN_FLAG
        child = child[1:]  # what frozen_entry forwards
    else:
        child = child[2:]  # past -m palctl.savescan
    assert child[0] == savescan.PRUNE_FLAG
    assert child[2] == f"{ZOE},{ANA}"


# ---------------- pruning one named player ----------------


def test_naming_a_player_narrows_the_selection(tmp_path):
    """The escape hatch the 90% refusal points at, and the way to deal with one
    enormous departed account without lowering the threshold for everybody."""
    world = make_world(tmp_path, {ZOE: 100, ANA: 100})
    seen = {ZOE: ("zoe", iso(200)), ANA: ("ana", iso(300))}

    plan = plan_prune(
        audit_of(world, seen), scan_of(**{ZOE: 10, ANA: 20}), only="zoe", now=NOW
    )

    assert [t.name for t in plan.targets] == ["zoe"]


def test_a_player_can_be_named_by_guid(tmp_path):
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(
        audit_of(world, {ZOE: ("zoe", iso(200))}),
        scan_of(**{ZOE: 10}),
        only=ZOE,
        now=NOW,
    )

    assert len(plan.targets) == 1


def test_naming_someone_never_widens_the_rules(tmp_path):
    """Naming a player is not a way around inactivity, identification or the
    exclusion list — it only narrows what was already eligible."""
    world = make_world(tmp_path, {ANA: 100})
    active = {ANA: ("ana", iso(1))}

    plan = plan_prune(
        audit_of(world, active), scan_of(**{ANA: 900}), only="ana", now=NOW
    )

    assert plan.targets == []
    assert not plan.safe  # and it says why rather than silently doing nothing


def test_naming_an_excluded_player_still_spares_them(tmp_path):
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(
        audit_of(world, {ZOE: ("zoe", iso(200))}),
        scan_of(**{ZOE: 10}),
        exclusions={ZOE},
        only="zoe",
        now=NOW,
    )

    assert plan.targets == []


def test_an_unmatched_name_says_so_rather_than_pruning_nobody_quietly(tmp_path):
    world = make_world(tmp_path, {ZOE: 100})
    plan = plan_prune(
        audit_of(world, {ZOE: ("zoe", iso(200))}),
        scan_of(**{ZOE: 10}),
        only="nobody-by-that-name",
        now=NOW,
    )

    assert not plan.safe
    assert "No inactive player matches" in plan.blockers[0]
