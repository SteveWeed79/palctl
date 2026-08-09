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
