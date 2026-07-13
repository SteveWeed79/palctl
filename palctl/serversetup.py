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
