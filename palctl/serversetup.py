"""
Get a freshly installed server ready for palctl in one call.

Palworld ships ``PalWorldSettings.ini`` blank and the REST API off, so every new
server needs the same three edits after the ini is seeded from the default:
``RESTAPIEnabled=True``, ``RESTAPIPort``, and ``AdminPassword``. The README made
you do these by hand. The wizard and installer do them with this — kept out of
the GUI, and therefore unit tested.
"""

from __future__ import annotations

from pathlib import Path

from .inifile import PalSettings, is_blank, seed_from_default


def ensure_rest_api(
    live_ini: Path,
    default_ini: Path,
    *,
    port: int,
    password: str,
) -> None:
    """
    Seed the ini if it's blank, then turn the REST API on and set the port and
    admin password so palctl can actually talk to the server.

    Raises FileNotFoundError if the ini is blank and there's no default to seed
    from — which almost always means the server isn't installed where we think.
    """
    if is_blank(live_ini):
        if not default_ini.exists():
            raise FileNotFoundError(
                f"Live ini is blank and {default_ini} is missing — "
                "is the dedicated server actually installed at that path?"
            )
        seed_from_default(default_ini, live_ini)

    settings = PalSettings.load(live_ini)
    settings.set("RESTAPIEnabled", True)
    settings.set("RESTAPIPort", int(port))
    if password:
        settings.set("AdminPassword", password)
    settings.save(live_ini)


# Gameplay settings the server copies into WorldOption.sav when a world is
# created, and reads from there afterwards. Server-infrastructure settings
# (ports, passwords, the REST API, the server name) keep coming from the ini.
# Used only to say *which* edits a present WorldOption.sav will swallow, so the
# warning is specific instead of "some settings might not apply".
_WORLD_OPTION_PREFIXES = (
    "Exp", "Pal", "Player", "Death", "Collection", "Enemy", "Build", "Drop",
    "Guild", "BaseCamp", "Day", "Night", "Difficulty", "Randomizer",
)


# Appended to memory warnings when raids are still on. Sourced, because it is
# advice about someone else's game and should be traceable: multiple independent
# hosting operators report that Palworld's scripted invader waves are the part
# of the leak that never gets cleaned up, and that turning them off roughly
# halves how fast RAM climbs. See docs/palworld-server-practices.md.
RAIDS_HINT = (
    " Raids (`bEnableInvaderEnemy`) are still enabled — their spawned waves are "
    "widely reported as the biggest single contributor to Palworld's memory "
    "leak, and turning them off roughly halves how fast memory climbs. It is a "
    "gameplay change, so palctl won't make it for you: it's in the settings "
    "editor under Combat."
)


def raids_enabled(live_ini: Path) -> bool | None:
    """Is `bEnableInvaderEnemy` on? None when it can't be determined.

    Deliberately tri-state: "we couldn't read the ini" must not be reported to
    the admin as "raids are on", which would be advice based on nothing.
    """
    try:
        value = PalSettings.load(live_ini).get("bEnableInvaderEnemy")
    except (OSError, ValueError):
        return None
    if value is None:
        return None
    return bool(value)


def find_world_option(savegames_dir: Path) -> Path | None:
    """The world's ``WorldOption.sav``, if one exists.

    This is the single most common reason Palworld server settings "don't
    apply". ``PalWorldSettings.ini`` is read when a world is first *created*;
    after that the server copies the gameplay settings into ``WorldOption.sav``
    inside the save folder and reads them from there. A world imported from
    co-op or single-player always brings one along. Editing the ini then changes
    nothing, with no error anywhere — the edit saves fine, the server just
    ignores it.

    Returns the first one found (a dedicated server has one world), or None.
    """
    try:
        return next(iter(sorted(savegames_dir.rglob("WorldOption.sav"))), None)
    except OSError:
        return None


def world_option_shadows(keys: list[str]) -> list[str]:
    """Which of `keys` a present WorldOption.sav is likely to override.

    Deliberately a heuristic on the name, not a hardcoded list: Palworld adds
    settings every patch, and a stale list would quietly under-report. Wrong in
    the safe direction — naming a setting that turns out to still work costs a
    needless mention, missing one costs a user hours of "why won't this apply".
    """
    return [k for k in keys if k.startswith(_WORLD_OPTION_PREFIXES)]


def restore_user_settings(live_ini: Path, backup_ini: Path) -> list[str]:
    """Put the user's pre-update values back over a live ini that a server
    update reset, and return the keys restored (empty when nothing changed).

    A SteamCMD run can leave PalWorldSettings.ini holding Palworld's *defaults*
    — a perfectly valid file, so the "is it blank?" check that guards the
    pre-update backup never fires and the backup is never used. The result is
    silent: every tuned rate back to 1.0, the server renamed, and — because the
    defaults have ``RESTAPIEnabled=False`` and no ``AdminPassword`` — palctl
    permanently blind to a server it can no longer authenticate to. Restarting
    doesn't help, because nothing is wrong with the process.

    This merges rather than overwrites, so a genuine game update isn't undone:
    keys the update *added* stay at their new defaults, keys the update
    *removed* stay removed, and only the values the user already had are put
    back. An untouched ini produces no changes at all, so the normal update path
    is unaffected.
    """
    try:
        live = PalSettings.load(live_ini)
        before = PalSettings.load(backup_ini)
    except (OSError, ValueError):
        return []  # nothing safe to say; the caller falls back to ensure_rest_api

    restored: list[str] = []
    for key in before.keys():
        if key not in live:
            continue  # the update dropped this setting; don't resurrect it
        old, new = before.option(key), live.option(key)
        if old.raw != new.raw:
            new.raw = old.raw  # raw text, so the value round-trips exactly
            restored.append(key)

    if restored:
        live.save(live_ini)
    return restored
