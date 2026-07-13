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
