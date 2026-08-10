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


# ---------------- WorldOption.sav ----------------
#
# The single most common reason Palworld settings "don't apply", and it fails
# silently: PalWorldSettings.ini is read when a world is CREATED; afterwards the
# server copies the gameplay settings into WorldOption.sav and reads them from
# there. Worlds imported from co-op or single-player always bring one along.

from palctl.serversetup import find_world_option, world_option_shadows  # noqa: E402


def test_no_world_option_is_the_quiet_answer(tmp_path):
    (tmp_path / "SaveGames").mkdir()
    assert find_world_option(tmp_path / "SaveGames") is None


def test_a_world_option_is_found_wherever_the_world_folder_is(tmp_path):
    """The world folder is a generated GUID, so it has to be searched for."""
    world = tmp_path / "SaveGames" / "0" / "A1B2C3D4"
    world.mkdir(parents=True)
    wo = world / "WorldOption.sav"
    wo.write_bytes(b"\x00")
    assert find_world_option(tmp_path / "SaveGames") == wo


def test_a_missing_savegames_dir_is_not_an_error(tmp_path):
    assert find_world_option(tmp_path / "nope") is None


def test_shadowed_keys_are_the_gameplay_ones_not_the_server_ones():
    keys = [
        "ExpRate", "PalCaptureRate", "DeathPenalty", "BaseCampMaxNum",
        "ServerName", "RESTAPIEnabled", "RESTAPIPort", "AdminPassword",
        "PublicPort",
    ]
    shadowed = world_option_shadows(keys)
    assert "ExpRate" in shadowed and "PalCaptureRate" in shadowed
    # The settings palctl itself depends on keep coming from the ini — if these
    # were shadowed, palctl could never reach the server at all.
    for infra in ("ServerName", "RESTAPIEnabled", "RESTAPIPort", "AdminPassword"):
        assert infra not in shadowed


# ---------------- raids and the memory leak ----------------


def test_raids_enabled_reads_the_setting(tmp_path):
    from palctl.serversetup import raids_enabled

    on = tmp_path / "on.ini"
    on.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(bEnableInvaderEnemy=True)\n",
        encoding="utf-8",
    )
    off = tmp_path / "off.ini"
    off.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(bEnableInvaderEnemy=False)\n",
        encoding="utf-8",
    )
    assert raids_enabled(on) is True
    assert raids_enabled(off) is False


def test_raids_enabled_is_none_when_it_cannot_be_determined(tmp_path):
    """Tri-state on purpose: "couldn't read the ini" must never be reported to
    the admin as "raids are on" — that's advice based on nothing."""
    from palctl.serversetup import raids_enabled

    assert raids_enabled(tmp_path / "missing.ini") is None

    no_key = tmp_path / "nokey.ini"
    no_key.write_text(
        "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ExpRate=1.000000)\n",
        encoding="utf-8",
    )
    assert raids_enabled(no_key) is None
