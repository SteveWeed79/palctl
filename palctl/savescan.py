"""
Weigh a `Level.sav`: how many of its records belong to whom.

`saveaudit` answers "which players stopped playing" from session history and
file sizes, and is careful to call its reclaimable figure a floor — because the
weight isn't in the small per-player `.sav` files, it's in those same players'
characters, pals, guilds and base camps *inside* `Level.sav`. This is the
module that opens the file and counts them.

**It always runs in a separate, short-lived process.** Two reasons, and the
second is the one that matters:

  1. Parsing a multi-gigabyte save needs multiple gigabytes of RAM. A daemon
     that does that in-process is a daemon the kernel's OOM killer may choose —
     taking down the supervisor of a running game server to answer a question
     nobody was blocked on.
  2. The parser is third-party code reading a format Pocketpair changes without
     notice. A malformed save must come back as "couldn't read it", not as an
     exception that escapes into the daemon's event loop.

So `scan()` shells out to `python -m palctl.savescan`, bounds it in time, and
treats *any* non-zero exit — crash, OOM kill, timeout, garbage on stdout — as
`ScanResult(ok=False)`. The caller degrades to the file-size view it already
has, which is exactly what `saveaudit` reports today.

Read-only, always. Nothing here writes a save.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Only the maps worth decoding. Everything else in the save — foliage grids, map
# models, item container slots — is left as raw bytes, which is the difference
# between a parse that fits in memory and one that does not. Character records
# and group (guild) records are what a cleanup would remove; base camps are
# counted because an abandoned one is the third source of bloat.
_WANTED_PROPERTIES = (
    ".worldSaveData.CharacterSaveParameterMap.Value.RawData",
    ".worldSaveData.GroupSaveDataMap",
)

# A save this size will not fit in a sensible amount of RAM once parsed, and the
# attempt is what gets the process killed. Refuse up front with a reason rather
# than being killed halfway through — a refusal is information, an OOM kill is
# a mystery. Generous, because the whole point is to help worlds that are big.
MAX_LEVEL_BYTES = 8 * 1024**3

# Parsing a few-GB save is minutes, not seconds. Bounded anyway: an unbounded
# subprocess is a wedged `palctl save-audit` with no output and no way forward.
SCAN_TIMEOUT_SECONDS = 900.0

# How the child is invoked when palctl is a frozen build.
#
# `sys.executable -m palctl.savescan` is correct from source and WRONG when
# frozen: sys.executable is palctl-daemon.exe, which has no -m, so the flag
# would start a second daemon instead of a save reader. That is the same shape
# as the health-task bug (see wintask.task_run_string) — a command that looks
# registered and cannot run — so the frozen entry points check for this marker
# argument before anything else and hand off to main().
SAVESCAN_FLAG = "--palctl-savescan"


def child_command(level_sav: Path) -> list[str]:
    """The argv that runs the save reader as a child of THIS build."""
    if getattr(sys, "frozen", False):
        return [sys.executable, SAVESCAN_FLAG, str(level_sav)]
    return [sys.executable, "-m", "palctl.savescan", str(level_sav)]


def frozen_entry(argv: list[str]) -> int | None:
    """For the frozen entry points: if this process was started as a save
    reader, run that and return an exit code; otherwise None and carry on.

    Lives here rather than in packaging/ so the marker and its handler cannot
    drift apart."""
    if len(argv) >= 2 and argv[0] == SAVESCAN_FLAG:
        return main([argv[1]])
    return None


@dataclass(frozen=True)
class ScanResult:
    """What `Level.sav` contains, or why palctl couldn't tell.

    `ok=False` is a normal outcome, not an error to raise on: an unreadable or
    enormous save is exactly the situation this exists to describe.
    """

    ok: bool
    error: str = ""
    characters: int = 0
    guilds: int = 0
    # playerId (upper-case, dashless) -> number of character records owned.
    # A player's own character plus every pal they caught, which is what makes
    # one long-departed player weigh more than another.
    by_player: dict[str, int] = field(default_factory=dict)
    # Character records whose owner is not a player GUID at all — wild pals and
    # anything the parser could not attribute. Never a cleanup target.
    unowned: int = 0

    @property
    def attributed(self) -> int:
        return sum(self.by_player.values())


def _normalise_guid(raw: object) -> str:
    """A GUID as palctl keys them: upper-case, no dashes.

    The save, the REST API and the filenames on disk each format these
    differently, and comparing them unnormalised silently attributes nothing —
    which reads as "this world has no player data" rather than as a bug.
    """
    return str(raw or "").replace("-", "").upper()


# The all-zero GUID is Palworld's "nobody": wild pals carry it. It must never
# look like a player whose records could be cleaned up.
_NOBODY = "0" * 32


def analyse(level_sav: Path) -> ScanResult:
    """Parse and count. Runs in the CHILD process — see the module docstring.

    Imports the vendored parser lazily so that merely importing this module
    (which the CLI does) costs nothing.
    """
    sys.path.insert(0, str(Path(__file__).parent / "vendor"))
    from palworld_save_tools.gvas import GvasFile
    from palworld_save_tools.palsav import decompress_sav_to_gvas
    from palworld_save_tools.paltypes import (
        PALWORLD_CUSTOM_PROPERTIES,
        PALWORLD_TYPE_HINTS,
    )

    size = level_sav.stat().st_size
    if size > MAX_LEVEL_BYTES:
        return ScanResult(
            ok=False,
            error=(
                f"Level.sav is {size / 1024**3:.1f} GB, past the "
                f"{MAX_LEVEL_BYTES / 1024**3:.0f} GB palctl will try to parse. "
                "Reading it would need more memory than this machine should "
                "spend on a report."
            ),
        )

    raw, _ = decompress_sav_to_gvas(level_sav.read_bytes())
    wanted = {
        k: v for k, v in PALWORLD_CUSTOM_PROPERTIES.items() if k in _WANTED_PROPERTIES
    }
    gvas = GvasFile.read(raw, PALWORLD_TYPE_HINTS, wanted, allow_nan=True)

    return count_records(gvas.properties.get("worldSaveData", {}).get("value", {}))


def count_records(world: dict) -> ScanResult:
    """Count and attribute the records in a parsed `worldSaveData`.

    Split out from `analyse` because this half is palctl's and has to be right —
    it decides whose records a future cleanup would remove — while the half
    above is a third-party parser reading a format that needs a real multi-GB
    save to exercise. This one is pure, so it is tested directly against the
    shapes the parser produces, including the malformed ones.
    """
    characters = world.get("CharacterSaveParameterMap", {}).get("value", []) or []
    groups = world.get("GroupSaveDataMap", {}).get("value", []) or []

    owned: Counter[str] = Counter()
    unowned = 0
    for entry in characters:
        if not isinstance(entry, dict):
            unowned += 1
            continue
        params = (
            _get(entry, "value", "RawData", "value", "object", "SaveParameter", "value")
            or {}
        )
        # A player's own character record is keyed by their PlayerUId; a pal
        # carries the GUID of whoever caught it in OwnerPlayerUId. Either way
        # the owner is the player whose leftovers this record is.
        owner = _get(params, "OwnerPlayerUId", "value")
        if owner is None:
            owner = _get(entry, "key", "PlayerUId", "value")
        guid = _normalise_guid(owner)
        if not guid or guid == _NOBODY:
            unowned += 1
        else:
            owned[guid] += 1

    return ScanResult(
        ok=True,
        characters=len(characters),
        guilds=len(groups),
        by_player=dict(owned),
        unowned=unowned,
    )


def _get(node: object, *path: str):
    """Walk a nested dict, giving up quietly at the first thing that isn't one.

    The parsed save is deeply nested and its shape moves between game patches.
    A chain of `.get()` calls raises AttributeError the moment one level comes
    back as a list or None — inside the child process, where the only thing the
    operator would see is "couldn't read Level.sav" for a save that is fine.
    """
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def scan(level_sav: Path, *, timeout: float = SCAN_TIMEOUT_SECONDS) -> ScanResult:
    """Weigh a Level.sav in a child process. Never raises.

    Runs in the PARENT. Every failure mode of the child — a crash, an OOM kill,
    a timeout, an upstream format change, JSON that isn't — comes back as
    `ok=False` with something to show the operator, because the caller's
    fallback (file sizes alone) is a perfectly good report and losing it to a
    traceback would be the worse outcome.
    """
    if not level_sav.is_file():
        return ScanResult(ok=False, error=f"No Level.sav at {level_sav}")

    try:
        proc = subprocess.run(
            child_command(level_sav),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ScanResult(
            ok=False,
            error=(
                f"Reading Level.sav took longer than {timeout / 60:.0f} minutes "
                "and was stopped."
            ),
        )
    except OSError as e:
        return ScanResult(ok=False, error=f"Couldn't start the save reader: {e}")

    # The child exits non-zero when it could not read the save — but it also
    # printed *why*, and that reason is far better than the exit code. Parse
    # stdout first and only fall back to the generic message when there is
    # nothing there, or this reports "exit code 1" for a child that carefully
    # explained the save was 9 GB.
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None

    if payload is None:
        if proc.returncode == 0:
            return ScanResult(
                ok=False, error="The save reader returned unreadable output."
            )
        # An OOM kill leaves no useful stdout and a negative return code; say so
        # in those terms rather than quoting a signal number at someone whose
        # server is stalling.
        killed = proc.returncode < 0
        detail = (proc.stderr or "").strip().splitlines()
        why = detail[-1] if detail else f"exit code {proc.returncode}"
        return ScanResult(
            ok=False,
            error=(
                "Ran out of memory reading Level.sav — it is too large for this "
                "machine to parse." if killed else f"Couldn't read Level.sav: {why}"
            ),
        )

    if not payload.get("ok"):
        return ScanResult(ok=False, error=str(payload.get("error", "unknown error")))

    return ScanResult(
        ok=True,
        characters=int(payload.get("characters", 0)),
        guilds=int(payload.get("guilds", 0)),
        by_player={str(k): int(v) for k, v in (payload.get("by_player") or {}).items()},
        unowned=int(payload.get("unowned", 0)),
    )


def main(argv: list[str] | None = None) -> int:
    """The child process: parse one save, print JSON, exit.

    Prints a JSON error rather than a traceback for anything it can catch, so
    the parent has something to show an operator either way.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(json.dumps({"ok": False, "error": "usage: palctl.savescan <Level.sav>"}))
        return 2
    try:
        result = analyse(Path(args[0]))
    except Exception as e:  # noqa: BLE001 — a third-party parser on a format that changes
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(
        json.dumps(
            {
                "ok": result.ok,
                "error": result.error,
                "characters": result.characters,
                "guilds": result.guilds,
                "by_player": result.by_player,
                "unowned": result.unowned,
            }
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
