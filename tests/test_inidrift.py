"""Drift detection for PalWorldSettings.ini.

The bug behind this module: a Steam update put Steam's defaults back over the
live ini, `RESTAPIEnabled` went to False, and palctl was permanently unable to
see a server that was running fine — reporting an outage that didn't exist. The
original fix asked "is the file blank?", which is not what had happened. So the
cases pinned here are the *variants*: blank, reset-to-defaults, hand-edited, and
gone — because guessing at the shape of the damage is exactly what failed.
"""

from pathlib import Path

import pytest

from palctl import inidrift
from palctl.inidrift import CRITICAL_KEYS

OPTIONS = (
    "RESTAPIEnabled={rest},RESTAPIPort=8212,AdminPassword=\"{pw}\","
    "ServerName=\"Ari's world\",DeathPenalty=None,bEnableInvaderEnemy={raids}"
)


def write_ini(path: Path, *, rest="True", pw="s3cret", raids="True") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        f"OptionSettings=({OPTIONS.format(rest=rest, pw=pw, raids=raids)})\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the snapshot into a temp config dir — it must never live next
    to the ini, where a Steam update would take the evidence with it."""
    monkeypatch.setattr(inidrift, "config_dir", lambda: tmp_path / "cfg")
    (tmp_path / "cfg").mkdir()
    return tmp_path


def test_a_file_palctl_wrote_reads_as_no_drift(home):
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    inidrift.record(ini)
    assert inidrift.compare(inidrift.load(), ini).kind == "none"


def test_a_steam_reset_is_reported_as_a_reset(home):
    """The one that actually happened. It is not blank, and every heuristic
    that looked for blankness missed it."""
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    inidrift.record(ini)

    default = write_ini(home / "server" / "DefaultPalWorldSettings.ini", rest="False", pw="")
    write_ini(ini, rest="False", pw="")  # the update copied defaults over the top

    drift = inidrift.compare(inidrift.load(), ini, default)
    assert drift.kind == "reset"
    assert "RESTAPIEnabled" in drift.critical
    assert "AdminPassword" in drift.critical
    assert "report the server as down" in inidrift.describe(drift)


def test_the_same_damage_without_a_default_to_compare_is_still_reported(home):
    """The default ini only refines the wording. Losing it must never turn
    drift into silence."""
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    inidrift.record(ini)
    write_ini(ini, rest="False", pw="")

    drift = inidrift.compare(inidrift.load(), ini)
    assert drift.kind == "edited"
    assert drift.matters
    assert "RESTAPIEnabled" in drift.critical


def test_a_hand_edit_of_a_game_setting_is_noted_but_not_critical(home):
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    inidrift.record(ini)
    write_ini(ini, raids="False")  # the admin turned raids off themselves

    drift = inidrift.compare(inidrift.load(), ini)
    assert drift.kind == "edited"
    assert drift.changed == ("bEnableInvaderEnemy",)
    assert drift.critical == ()
    assert "nothing needs doing" in inidrift.describe(drift)


def test_a_missing_or_empty_file_is_reported_as_critical(home):
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    inidrift.record(ini)
    saved = inidrift.load()

    ini.write_text("", encoding="utf-8")
    blank = inidrift.compare(saved, ini)
    assert blank.kind == "blank" and blank.critical == CRITICAL_KEYS

    ini.unlink()
    gone = inidrift.compare(saved, ini)
    assert gone.kind == "missing" and gone.critical == CRITICAL_KEYS
    assert "gone" in inidrift.describe(gone)


def test_a_setting_that_disappeared_counts_as_lost_not_changed(home):
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    inidrift.record(ini)
    ini.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(RESTAPIEnabled=True,RESTAPIPort=8212,AdminPassword="s3cret")\n',
        encoding="utf-8",
    )
    drift = inidrift.compare(inidrift.load(), ini)
    assert "ServerName" in drift.lost
    assert drift.changed == ()


def test_no_snapshot_is_not_drift(home):
    """A first run, or an upgrade from a palctl that never took one. Reporting
    drift there would cry wolf on every fresh install."""
    ini = write_ini(home / "server" / "PalWorldSettings.ini")
    drift = inidrift.compare(None, ini)
    assert drift.kind == "no_snapshot"
    assert not drift.matters
    assert inidrift.describe(drift) == ""


def test_the_snapshot_never_stores_a_secret(home):
    """SECURITY.md: anything that writes a secret to disk in the clear is a
    vulnerability. This file sits next to the config and would be exactly that
    if it kept values instead of hashes."""
    ini = write_ini(home / "server" / "PalWorldSettings.ini", pw="hunter2-in-the-clear")
    inidrift.record(ini)
    raw = inidrift.snapshot_path().read_text(encoding="utf-8")
    assert "hunter2" not in raw
    assert "AdminPassword" in raw  # the key is tracked, the value is not stored


def test_a_corrupt_snapshot_degrades_to_no_snapshot(home):
    inidrift.snapshot_path().write_text("{not json", encoding="utf-8")
    assert inidrift.load() is None
    inidrift.snapshot_path().write_text('{"path": 1}', encoding="utf-8")
    assert inidrift.load() is None
