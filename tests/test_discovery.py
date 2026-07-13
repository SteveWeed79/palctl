"""Path detection is best-effort, but the validation and vdf parsing under it
must be exact — a false positive points palctl at the wrong folder and every
backup/restore/update after that is aimed at the wrong place."""

from pathlib import Path

from palctl import discovery
from palctl.discovery import (
    detect_server_roots,
    is_server_root,
    is_steamcmd,
    parse_library_folders,
)

# A realistic newer-Steam libraryfolders.vdf: KeyValues, backslash-escaped paths.
VDF = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"apps"
		{
			"2394010"		"1234567"
		}
	}
	"1"
	{
		"path"		"D:\\SteamLibrary"
		"label"		""
	}
}
'''


def test_parse_library_folders_unescapes_and_orders():
    roots = parse_library_folders(VDF)
    assert roots == [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"D:\SteamLibrary"),
    ]


def test_parse_library_folders_empty():
    assert parse_library_folders("") == []


def test_is_server_root_via_default_ini(tmp_path: Path):
    assert not is_server_root(tmp_path)  # empty dir
    (tmp_path / "DefaultPalWorldSettings.ini").write_text("x", encoding="utf-8")
    assert is_server_root(tmp_path)


def test_is_server_root_via_shipping_binary(tmp_path: Path):
    win64 = tmp_path / "Pal" / "Binaries" / "Win64"
    win64.mkdir(parents=True)
    (win64 / "PalServer-Win64-Shipping.exe").write_bytes(b"MZ")
    assert is_server_root(tmp_path)


def test_is_server_root_rejects_nonexistent_and_files(tmp_path: Path):
    assert not is_server_root(tmp_path / "nope")
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    assert not is_server_root(f)


def test_is_steamcmd(tmp_path: Path):
    exe = tmp_path / "steamcmd.exe"
    exe.write_bytes(b"MZ")
    assert is_steamcmd(exe)
    assert is_steamcmd(tmp_path / "SteamCMD.EXE".lower())  # case-insensitive name
    assert not is_steamcmd(tmp_path)  # a directory
    assert not is_steamcmd(tmp_path / "steam.exe")  # wrong name
    assert not is_steamcmd(tmp_path / "missing" / "steamcmd.exe")


def test_is_steamcmd_accepts_linux_names(tmp_path: Path):
    sh = tmp_path / "steamcmd.sh"
    sh.write_text("#!/bin/sh\n", encoding="utf-8")
    assert is_steamcmd(sh)


def test_is_server_root_linux_via_palserver_sh(tmp_path: Path):
    (tmp_path / "PalServer.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert is_server_root(tmp_path)


def test_detect_server_roots_dedups_and_validates(tmp_path: Path, monkeypatch):
    real = tmp_path / "PalServer"
    real.mkdir()
    (real / "PalServer.exe").write_bytes(b"MZ")
    fake = tmp_path / "NotAServer"
    fake.mkdir()

    # No process, no registry: drive detection entirely off the "common dirs"
    # list, with one valid path listed twice and one invalid path. Patch both
    # platforms' lists so the test is OS-agnostic.
    monkeypatch.setattr(discovery, "server_root_from_process", lambda: None)
    monkeypatch.setattr(discovery, "_steam_library_roots", list)
    for attr in ("_COMMON_SERVER_DIRS_WIN", "_COMMON_SERVER_DIRS_LINUX"):
        monkeypatch.setattr(discovery, attr, (str(real), str(real), str(fake)))

    roots = detect_server_roots()
    assert roots == [real]  # valid, de-duplicated; the invalid one dropped


def test_detect_server_roots_prefers_running_process(tmp_path: Path, monkeypatch):
    proc_root = tmp_path / "FromProcess"
    proc_root.mkdir()
    (proc_root / "DefaultPalWorldSettings.ini").write_text("x", encoding="utf-8")
    common = tmp_path / "FromCommon"
    common.mkdir()
    (common / "PalServer.exe").write_bytes(b"MZ")

    monkeypatch.setattr(discovery, "server_root_from_process", lambda: proc_root)
    monkeypatch.setattr(discovery, "_steam_library_roots", list)
    for attr in ("_COMMON_SERVER_DIRS_WIN", "_COMMON_SERVER_DIRS_LINUX"):
        monkeypatch.setattr(discovery, attr, (str(common),))

    assert detect_server_roots()[0] == proc_root  # process root wins
