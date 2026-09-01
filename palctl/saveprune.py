"""
Remove departed players' records from `Level.sav`.

This is the only code in palctl that rewrites a world, so it is built as a
series of refusals with a mutation at the end rather than a mutation with some
checks bolted on.

**The rules, in the order they are enforced.** Every one of them has a test,
and each exists because skipping it deletes something somebody wanted:

  1. **Dry run unless told otherwise.** `apply=False` is the default everywhere
     — module, CLI, and any future surface. You have to ask twice.
  2. **The server must be stopped.** Rewriting a save the server has open
     corrupts it, and Palworld will happily overwrite the result on its next
     autosave anyway. The caller proves this; `run_prune` refuses without it.
  3. **A fresh backup, verified.** Not "a backup exists" — one taken now, and
     passed through `backups.verify()`, which is what 1.3 built. A prune whose
     undo is a backup nobody checked is not an undo.
  4. **Only known, inactive, non-excluded players.** A save file palctl cannot
     match to a session is never a target (see saveaudit's rule 1), and neither
     is anyone on the exclusion list.
  5. **The rewrite happens out of process,** for the same reason the scan does:
     a multi-gigabyte parse must not be able to take the daemon down.
  6. **The new save is re-read before it replaces anything.** The child writes
     to a temporary file, re-parses it, and confirms the targets are gone *and*
     that everyone else's record count is unchanged. Only then is the file
     swapped in, and the original is kept beside it.

The exclusion list is what makes this safe to run more than once: a player
removed today who comes back tomorrow is a *new* character to the game, and
without a record of who was pruned there is no way to tell "returned" from
"never left" on the next run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import backups, saveaudit, savescan

# Where the list of players palctl has already pruned, or been told never to
# prune, lives. Beside the world rather than in palctl's config: it describes
# *this world*, and it has to survive palctl being reinstalled or the world
# being moved to another box.
EXCLUSIONS_NAME = "palctl-prune-exclusions.json"


@dataclass(frozen=True)
class PruneTarget:
    player_id: str
    name: str
    records: int
    last_seen_days: float


@dataclass(frozen=True)
class PrunePlan:
    """Who would be removed, and everything that argues against doing it."""

    targets: list[PruneTarget] = field(default_factory=list)
    # Players who qualified on inactivity but are protected — already pruned,
    # or listed by the operator. Reported, never removed.
    excluded: list[str] = field(default_factory=list)
    # Save files palctl cannot identify. Counted so the operator can see how
    # much of their world this will never be able to help with.
    unknown: int = 0
    blockers: list[str] = field(default_factory=list)

    @property
    def records(self) -> int:
        return sum(t.records for t in self.targets)

    @property
    def safe(self) -> bool:
        return not self.blockers and bool(self.targets)


def load_exclusions(world: Path) -> set[str]:
    """Player GUIDs that must never be pruned.

    Returns what it can read. A file that exists but does not parse comes back
    empty here, which would *widen* the target set — so callers must gate on
    `exclusions_readable()` first and treat that case as a refusal rather than
    as "nobody is protected".
    """
    try:
        data = json.loads((world / EXCLUSIONS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    return {str(k).replace("-", "").upper() for k in (data.get("players") or {})}


def exclusions_readable(world: Path) -> bool:
    """Whether the exclusion file is absent (fine) or parseable (fine).

    A file that exists and cannot be read is the dangerous case: it means
    palctl cannot tell who is already protected, and proceeding would re-prune
    a returning player. That is a blocker, not a warning.
    """
    path = world / EXCLUSIONS_NAME
    if not path.exists():
        return True
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        return False


def record_exclusions(world: Path, targets: list[PruneTarget]) -> None:
    """Remember who was pruned, so a returning player is never pruned twice."""
    path = world / EXCLUSIONS_NAME
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        players = existing.get("players") or {}
    except (OSError, ValueError):
        players = {}
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    for t in targets:
        players[t.player_id] = {
            "name": t.name,
            "pruned_at": stamp,
            "records": t.records,
        }
    path.write_text(json.dumps({"players": players}, indent=1), encoding="utf-8")


def plan_prune(
    audit: saveaudit.SaveAudit,
    scan: savescan.ScanResult,
    *,
    days: int = saveaudit.INACTIVE_DAYS,
    exclusions: set[str] | None = None,
    now: datetime | None = None,
) -> PrunePlan:
    """Decide who would be removed. Pure — no files, no processes, no clock.

    Everything that makes this dangerous is decided here, which is why it is
    separated from the code that does the removing and tested on its own.
    """
    moment = now or datetime.now(UTC)
    protected = exclusions or set()
    blockers: list[str] = []

    if not scan.ok:
        blockers.append(
            f"Level.sav could not be read ({scan.error}) — palctl will not "
            "remove records it cannot count first."
        )

    targets: list[PruneTarget] = []
    excluded: list[str] = []
    for p in audit.inactive(days=days, now=moment):
        guid = p.player_id.replace("-", "").upper()
        if guid in protected:
            excluded.append(p.name or p.player_id)
            continue
        records = scan.by_player.get(guid, 0)
        if not records:
            # Known and idle, but nothing of theirs is in Level.sav. Removing
            # nothing is not worth the risk of a rewrite.
            continue
        targets.append(
            PruneTarget(
                player_id=guid,
                name=p.name,
                records=records,
                last_seen_days=p.inactive_for_days(moment) or 0.0,
            )
        )

    if scan.ok and targets and scan.characters and (
        sum(t.records for t in targets) > scan.characters * 0.9
    ):
        # A plan that removes almost everything is far more likely to be a bug
        # in attribution than a world where 90% of the records are abandoned.
        blockers.append(
            f"The plan would remove {sum(t.records for t in targets):,} of "
            f"{scan.characters:,} character records. That is too much of the "
            "world to remove on an automatic judgement — check `save-audit "
            "--deep` and prune specific players instead."
        )

    return PrunePlan(
        targets=targets,
        excluded=excluded,
        unknown=len(audit.unknown),
        blockers=blockers,
    )


def format_plan(plan: PrunePlan, *, applied: bool = False) -> str:
    lines: list[str] = []
    if plan.blockers:
        lines.append("Refusing to prune:")
        lines += [f"  ✗ {b}" for b in plan.blockers]
        return "\n".join(lines)

    if not plan.targets:
        return "Nothing to prune — no inactive player has records in Level.sav."

    verb = "Removed" if applied else "Would remove"
    lines.append(f"{verb} {plan.records:,} character record(s):")
    for t in sorted(plan.targets, key=lambda t: t.records, reverse=True):
        lines.append(
            f"  {t.name or t.player_id:<24} {t.records:>6,} record(s), "
            f"last seen {t.last_seen_days:,.0f} days ago"
        )
    if plan.excluded:
        lines.append(
            f"\n  Skipped {len(plan.excluded)} protected player(s): "
            + ", ".join(plan.excluded)
        )
    if plan.unknown:
        lines.append(
            f"  {plan.unknown} save file(s) palctl can't identify were not "
            "considered — it never removes records it cannot attribute."
        )
    if not applied:
        lines.append("\nThis was a dry run. Nothing was changed.")
    return "\n".join(lines)


@dataclass(frozen=True)
class PruneOutcome:
    ok: bool
    plan: PrunePlan
    applied: bool = False
    backup: str = ""
    error: str = ""
    message: str = ""


def run_prune(
    world: Path,
    savegames: Path,
    backup_root: Path,
    seen: dict[str, tuple[str, str]],
    *,
    server_stopped: bool,
    apply: bool = False,
    days: int = saveaudit.INACTIVE_DAYS,
    now: datetime | None = None,
) -> PruneOutcome:
    """Plan a prune, and — only with `apply=True` — carry it out.

    The caller owns stopping the server and holding the operation lock. Both
    are asserted rather than assumed: this function is reachable from a CLI
    that has neither.
    """
    if not exclusions_readable(world):
        plan = PrunePlan(blockers=[
            f"{EXCLUSIONS_NAME} exists but could not be read. palctl can't tell "
            "who is already protected, and pruning without that list would "
            "remove a returning player's records a second time."
        ])
        return PruneOutcome(ok=False, plan=plan, error=plan.blockers[0])

    audit = saveaudit.audit(world, seen)
    scan = savescan.scan(world / saveaudit.LEVEL_SAV)
    plan = plan_prune(
        audit, scan, days=days, exclusions=load_exclusions(world), now=now
    )

    if not apply:
        return PruneOutcome(ok=True, plan=plan, applied=False)

    if plan.blockers:
        return PruneOutcome(ok=False, plan=plan, error=plan.blockers[0])
    if not plan.targets:
        return PruneOutcome(ok=True, plan=plan, message="Nothing to prune.")
    if not server_stopped:
        return PruneOutcome(
            ok=False,
            plan=plan,
            error=(
                "The server is still running. Rewriting Level.sav underneath it "
                "corrupts the world, and its next autosave would overwrite the "
                "result anyway. Stop the server first."
            ),
        )

    # Rule 3: a fresh backup, and one that has been checked. `backups.verify`
    # is what makes this an undo rather than a hope.
    try:
        backup = backups.create(savegames, backup_root, "pre-prune", flushed=True)
    except Exception as e:  # noqa: BLE001 — any failure here means do not proceed
        return PruneOutcome(
            ok=False, plan=plan, error=f"Couldn't take a backup first: {e}"
        )
    report = backups.verify(backup_root, backup.name)
    if not report.ok:
        return PruneOutcome(
            ok=False,
            plan=plan,
            backup=backup.name,
            error=(
                "The pre-prune backup did not verify ("
                + "; ".join(report.problems[:3])
                + "). Nothing was changed."
            ),
        )

    result = savescan.prune_records(
        world / saveaudit.LEVEL_SAV, [t.player_id for t in plan.targets]
    )
    if not result.get("ok"):
        return PruneOutcome(
            ok=False,
            plan=plan,
            backup=backup.name,
            error=str(result.get("error", "the rewrite failed")),
        )

    record_exclusions(world, plan.targets)
    return PruneOutcome(
        ok=True,
        plan=plan,
        applied=True,
        backup=backup.name,
        message=(
            f"Removed {result.get('removed', 0):,} record(s). The world before "
            f"this is backed up as {backup.name}, and the original Level.sav is "
            f"beside the new one as {result.get('original', 'Level.sav.pre-prune')}."
        ),
    )
