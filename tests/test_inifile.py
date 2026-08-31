"""The ini round-trip is the highest-stakes code in the project: a parsing bug
here rewrites someone's PalWorldSettings.ini wrong and eats their server."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from palctl import inifile
from palctl.inifile import (
    PalSettings,
    ValueKind,
    _classify,
    _split_top_level,
    is_blank,
    read_admin_password,
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


def test_read_admin_password_from_live_ini(tmp_path: Path):
    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text(SAMPLE, encoding="utf-8")
    assert read_admin_password(ini) == "s3cret"


def test_read_admin_password_missing_or_blank(tmp_path: Path):
    assert read_admin_password(tmp_path / "nope.ini") == ""
    blank = tmp_path / "blank.ini"
    blank.write_text("", encoding="utf-8")
    assert read_admin_password(blank) == ""


# ---------------- the file is more than the OptionSettings line ----------------
#
# Finding the end of the block by regex ("everything up to the last ')' at end
# of file") broke on two shapes of real file. Both matter because this module
# writes the file the game actually boots from.


def test_a_trailing_comment_does_not_hide_the_whole_block():
    """A line after the block used to make the anchored regex fail outright, so
    a perfectly good ini read as 'no OptionSettings block' — which the user is
    told means the file is blank and should be re-seeded from the default. That
    advice would have destroyed the settings palctl had just failed to read."""
    text = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(Difficulty=None,MaxPlayers=32)\n'
        "; remember to raise ExpRate later\n"
    )
    s = PalSettings.parse(text)
    assert s.get("MaxPlayers") == 32
    assert "; remember to raise ExpRate later" in s.render()


def test_admin_password_survives_a_trailing_comment(tmp_path):
    """The quiet consequence of the above: read_admin_password swallows the
    parse error and returns '', so the daemon can't authenticate to the REST API
    and reports the server as up-but-unauthorised, for a one-line comment."""
    from palctl.inifile import read_admin_password

    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(AdminPassword="hunter2")\n'
        "; a note\n",
        encoding="utf-8",
    )
    assert read_admin_password(ini) == "hunter2"


def test_a_second_section_is_neither_swallowed_nor_rewritten():
    """The greedy match reached past the block to the last ')' in the file, so
    another section landed *inside* the final option's value — and writing back
    mangled both. Content outside the block must round-trip untouched."""
    text = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(Difficulty=None,MaxPlayers=32)\n'
        "\n"
        "[/Script/Pal.SomethingElse]\n"
        "Foo=(1,2)\n"
    )
    s = PalSettings.parse(text)
    assert s.get("MaxPlayers") == 32, "the last option must not absorb the next section"
    assert s.render() == text, "everything outside the block must survive verbatim"


def test_editing_a_value_keeps_the_rest_of_the_file():
    text = (
        "; palctl notes\n"
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(Difficulty=None,MaxPlayers=32)\n'
        "\n[/Script/Pal.Other]\nFoo=(1,2)\n"
    )
    s = PalSettings.parse(text)
    s.set("MaxPlayers", 64)
    out = s.render()
    assert "MaxPlayers=64" in out
    assert out.startswith("; palctl notes\n")
    assert out.endswith("\n[/Script/Pal.Other]\nFoo=(1,2)\n")


def test_a_closing_paren_inside_a_quoted_string_does_not_end_the_block():
    text = (
        "[/Script/Pal.PalGameWorldSettings]\n"
        'OptionSettings=(ServerDescription="closing ) paren",MaxPlayers=32)\n'
    )
    s = PalSettings.parse(text)
    assert s.get("MaxPlayers") == 32
    assert s.get("ServerDescription") == "closing ) paren"


def test_a_truncated_block_still_yields_what_survived():
    """A half-written file (killed mid-save) should give back the options that
    made it, not raise — the caller can still show and re-save them."""
    text = "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(Difficulty=None,MaxPlay"
    s = PalSettings.parse(text)
    assert s.get("Difficulty") == "None"


# ---------------- .bak retention beside the live ini ----------------


class _TickingClock:
    """A distinct %Y%m%d-%H%M%S stamp per call, so a burst of saves produces a
    burst of .bak files the way a run of updates does over days — not one file
    repeatedly overwritten because the test was faster than a second."""

    def __init__(self):
        self.n = 0

    def now(self):
        self.n += 1
        return datetime(2026, 1, 1) + timedelta(seconds=self.n)


def test_saving_keeps_a_bounded_number_of_bak_copies(tmp_path, monkeypatch):
    """Every path that rewrites PalWorldSettings.ini takes a .bak first, and a
    single server update takes up to three (the pre-update snapshot,
    restore_user_settings, ensure_rest_api). Nothing ever removed them, so with
    scheduled auto-updates on that is roughly a thousand files a year piling up
    in the server's own Config folder."""
    monkeypatch.setattr(inifile, "datetime", _TickingClock())
    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text(
        '[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName="a")\n',
        encoding="utf-8",
    )
    saves = inifile.BACKUP_RETAIN + 8
    for i in range(saves):
        s = PalSettings.load(ini)
        s.set("ServerName", f"name{i}")
        s.save(ini)

    baks = sorted(tmp_path.glob("PalWorldSettings.ini.*.bak"))
    assert len(baks) == inifile.BACKUP_RETAIN
    # The live file is still the last thing written, untouched by the trim.
    assert f"name{saves - 1}" in ini.read_text(encoding="utf-8")
    # ...and what survived is the most recent history, not a random slice: the
    # newest .bak is the copy taken just before the final save.
    assert f"name{saves - 2}" in baks[-1].read_text(encoding="utf-8")


def test_the_newest_bak_copies_are_the_ones_kept(tmp_path):
    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text("live", encoding="utf-8")
    made = [
        tmp_path / f"PalWorldSettings.ini.2026010{i}-000000.bak" for i in range(1, 9)
    ]
    for p in made:
        p.write_text("old", encoding="utf-8")

    inifile.prune_backups(ini, retain=3)
    left = sorted(p.name for p in tmp_path.glob("PalWorldSettings.ini.*.bak"))
    assert left == [p.name for p in made[-3:]]
    assert ini.read_text(encoding="utf-8") == "live"


def test_pruning_bak_copies_never_raises_on_an_undeletable_one(tmp_path, monkeypatch):
    """The copy is the point and is never skipped; the trim is housekeeping and
    must not fail the save it was taken to protect."""
    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text("live", encoding="utf-8")
    for i in range(1, 6):
        (tmp_path / f"PalWorldSettings.ini.2026010{i}-000000.bak").write_text("x")

    def _no(self, *a, **kw):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", _no)
    assert inifile.prune_backups(ini, retain=1) == []


def test_a_backup_is_taken_even_when_the_folder_is_already_full_of_them(tmp_path):
    ini = tmp_path / "PalWorldSettings.ini"
    ini.write_text("live", encoding="utf-8")
    bak = inifile.timestamped_backup(ini, retain=1)
    assert bak.exists() and bak.read_text(encoding="utf-8") == "live"
