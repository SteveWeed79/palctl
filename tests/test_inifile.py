"""The ini round-trip is the highest-stakes code in the project: a parsing bug
here rewrites someone's PalWorldSettings.ini wrong and eats their server."""

from pathlib import Path

import pytest

from palctl.inifile import (
    PalSettings,
    ValueKind,
    _classify,
    _split_top_level,
    is_blank,
    seed_from_default,
)

SAMPLE = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    "OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,"
    'ServerName="My Server",ServerDescription="Hi, welcome! (beta)",'
    "ServerPlayerMaxNum=32,bEnableInvaderEnemy=True,RESTAPIEnabled=False,"
    "CrossplayPlatforms=(Steam,Xbox,PS5,Mac),ExpRate=1.000000,"
    'AdminPassword="s3cret",FutureUnknownKey=SomethingNew)\n'
)


def test_split_ignores_commas_in_quotes_and_parens():
    parts = _split_top_level('A=1,B="x, y",C=(a,b,c),D=2.5')
    assert parts == ["A=1", 'B="x, y"', "C=(a,b,c)", "D=2.5"]


def test_classify():
    assert _classify("True") == ValueKind.BOOL
    assert _classify("false") == ValueKind.BOOL
    assert _classify("32") == ValueKind.INT
    assert _classify("-4") == ValueKind.INT
    assert _classify("1.000000") == ValueKind.FLOAT
    assert _classify('"hello"') == ValueKind.STRING
    assert _classify("(Steam,Xbox)") == ValueKind.TUPLE
    assert _classify("None") == ValueKind.ENUM


def test_parse_types_and_values():
    s = PalSettings.parse(SAMPLE)
    assert s.get("Difficulty") == "None"
    assert s.get("DayTimeSpeedRate") == 1.0
    assert s.get("ServerName") == "My Server"
    assert s.get("ServerDescription") == "Hi, welcome! (beta)"
    assert s.get("ServerPlayerMaxNum") == 32
    assert s.get("bEnableInvaderEnemy") is True
    assert s.get("RESTAPIEnabled") is False
    assert s.get("CrossplayPlatforms") == ["Steam", "Xbox", "PS5", "Mac"]


def test_round_trip_is_lossless():
    s = PalSettings.parse(SAMPLE)
    assert PalSettings.parse(s.render()).render() == s.render()
    # Untouched values keep their exact original text, including the
    # quoted comma and the unknown key from a "future patch".
    assert 'ServerDescription="Hi, welcome! (beta)"' in s.render()
    assert "FutureUnknownKey=SomethingNew" in s.render()


def test_set_preserves_formatting_conventions():
    s = PalSettings.parse(SAMPLE)
    s.set("ExpRate", 2.5)
    assert "ExpRate=2.500000" in s.render()  # Palworld's 6-decimal style
    s.set("bEnableInvaderEnemy", False)
    assert "bEnableInvaderEnemy=False" in s.render()
    s.set("ServerName", 'New "Name"')
    assert s.get("ServerName") == "New Name"  # embedded quotes stripped
    s.set("CrossplayPlatforms", ["Steam", "Xbox"])
    assert "CrossplayPlatforms=(Steam,Xbox)" in s.render()


def test_set_unknown_key_appends():
    s = PalSettings.parse(SAMPLE)
    s.set("BrandNewKey", 7)
    assert s.keys()[-1] == "BrandNewKey"
    assert "BrandNewKey=7" in s.render()


def test_parse_rejects_blank_file():
    with pytest.raises(ValueError):
        PalSettings.parse("[/Script/Pal.PalGameWorldSettings]\n")


def test_save_takes_backup(tmp_path: Path):
    live = tmp_path / "PalWorldSettings.ini"
    live.write_text(SAMPLE, encoding="utf-8")

    s = PalSettings.load(live)
    s.set("ServerPlayerMaxNum", 16)
    bak = s.save(live)

    assert bak is not None and bak.exists()
    assert "OptionSettings" in bak.read_text(encoding="utf-8")
    assert "ServerPlayerMaxNum=16" in live.read_text(encoding="utf-8")


def test_is_blank_and_seed(tmp_path: Path):
    live = tmp_path / "cfg" / "PalWorldSettings.ini"
    default = tmp_path / "DefaultPalWorldSettings.ini"
    default.write_text(SAMPLE, encoding="utf-8")

    assert is_blank(live)  # missing counts as blank
    live.parent.mkdir(parents=True)
    live.write_text("", encoding="utf-8")
    assert is_blank(live)

    seed_from_default(default, live)
    assert not is_blank(live)
    assert PalSettings.load(live).get("ServerName") == "My Server"


def test_load_handles_utf8_bom(tmp_path: Path):
    live = tmp_path / "PalWorldSettings.ini"
    live.write_bytes(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
    assert PalSettings.load(live).get("ServerName") == "My Server"
