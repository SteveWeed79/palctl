"""Enabling the REST API is the one edit that makes the whole tool able to talk
to the server, so the seed-then-set flow is tested end to end on a real ini."""

from pathlib import Path

import pytest

from palctl.inifile import PalSettings
from palctl.serversetup import ensure_rest_api

DEFAULT = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    "OptionSettings=(Difficulty=None,ServerName=\"Default\","
    "RESTAPIEnabled=False,RESTAPIPort=8212,AdminPassword=\"\")\n"
)


def test_seeds_blank_ini_then_enables(tmp_path: Path):
    default_ini = tmp_path / "DefaultPalWorldSettings.ini"
    default_ini.write_text(DEFAULT, encoding="utf-8")
    live = tmp_path / "cfg" / "PalWorldSettings.ini"  # missing == blank

    ensure_rest_api(live, default_ini, port=9999, password="hunter2")

    s = PalSettings.load(live)
    assert s.get("RESTAPIEnabled") is True
    assert s.get("RESTAPIPort") == 9999
    assert s.get("AdminPassword") == "hunter2"
    # Seeded from the default, so unrelated keys survive.
    assert s.get("Difficulty") == "None"


def test_updates_existing_ini_without_reseeding(tmp_path: Path):
    default_ini = tmp_path / "DefaultPalWorldSettings.ini"
    default_ini.write_text(DEFAULT, encoding="utf-8")
    live = tmp_path / "PalWorldSettings.ini"
    live.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ServerName="Mine",RESTAPIEnabled=False,ExpRate=3.000000)\n',
        encoding="utf-8",
    )

    ensure_rest_api(live, default_ini, port=8212, password="")

    s = PalSettings.load(live)
    assert s.get("RESTAPIEnabled") is True
    assert s.get("ServerName") == "Mine"  # not clobbered by the default
    assert s.get("ExpRate") == 3.0
    assert s.get("AdminPassword") is None  # empty password left alone


def test_blank_ini_without_default_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ensure_rest_api(
            tmp_path / "PalWorldSettings.ini",
            tmp_path / "DefaultPalWorldSettings.ini",  # missing
            port=8212,
            password="x",
        )


# ---------------- putting settings back after an update reset them ----------
#
# The pre-update backup was only ever used when the ini came back *blank*. A
# server update that leaves a valid ini full of Palworld's defaults slipped
# through: every tuned rate reverted, and — since the defaults carry
# RESTAPIEnabled=False and no AdminPassword — palctl went permanently blind to a
# server that was running fine. Restarting changed nothing, because the process
# was never the problem.

from palctl.serversetup import restore_user_settings  # noqa: E402

_TUNED = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(ExpRate=3.000000,ServerName="mine",AdminPassword="hunter2",'
    "RESTAPIEnabled=True,ServerPlayerMaxNum=32)\n"
)
_DEFAULTS = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(ExpRate=1.000000,ServerName="Default Palworld Server",'
    'AdminPassword="",RESTAPIEnabled=False,ServerPlayerMaxNum=32)\n'
)


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    return p


def test_a_reset_ini_gets_the_admins_values_back(tmp_path):
    live = _write(tmp_path / "live.ini", _DEFAULTS)
    backup = _write(tmp_path / "live.ini.bak", _TUNED)

    restored = restore_user_settings(live, backup)

    assert set(restored) == {"ExpRate", "ServerName", "AdminPassword", "RESTAPIEnabled"}
    after = PalSettings.load(live)
    assert after.get("ExpRate") == 3.0
    assert after.get("AdminPassword") == "hunter2"
    assert after.get("RESTAPIEnabled") is True
    # A value that never changed isn't reported as restored.
    assert "ServerPlayerMaxNum" not in restored


def test_an_untouched_ini_is_left_completely_alone(tmp_path):
    """The normal update path: Steam didn't touch the ini, so nothing happens
    and the admin gets no misleading 'we put your settings back' message."""
    live = _write(tmp_path / "live.ini", _TUNED)
    backup = _write(tmp_path / "live.ini.bak", _TUNED)
    before = live.read_text(encoding="utf-8")

    assert restore_user_settings(live, backup) == []
    assert live.read_text(encoding="utf-8") == before


def test_a_setting_the_game_update_added_keeps_its_new_default(tmp_path):
    """Merge, not overwrite — restoring the backup wholesale would delete
    settings a genuine Palworld patch introduced."""
    live = _write(
        tmp_path / "live.ini",
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ExpRate=1.000000,BrandNewSetting=7)\n',
    )
    backup = _write(
        tmp_path / "live.ini.bak",
        "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ExpRate=3.000000)\n",
    )

    restored = restore_user_settings(live, backup)

    after = PalSettings.load(live)
    assert restored == ["ExpRate"]
    assert after.get("ExpRate") == 3.0
    assert after.get("BrandNewSetting") == 7, "a new setting must survive"


def test_a_setting_the_game_update_removed_is_not_resurrected(tmp_path):
    live = _write(
        tmp_path / "live.ini",
        "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ExpRate=1.000000)\n",
    )
    backup = _write(
        tmp_path / "live.ini.bak",
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(ExpRate=3.000000,RetiredSetting=1)\n",
    )

    restore_user_settings(live, backup)

    assert "RetiredSetting" not in PalSettings.load(live)


def test_an_unreadable_backup_is_survivable(tmp_path):
    """Never raise into the update's finally block — the server still has to be
    started again afterwards."""
    live = _write(tmp_path / "live.ini", _DEFAULTS)
    assert restore_user_settings(live, tmp_path / "missing.bak") == []
