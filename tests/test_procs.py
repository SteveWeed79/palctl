"""Service control is the one Windows-only layer, now split across sc.exe and
systemd. The parsers and command builders are pure, so both platforms' logic is
checked on whatever OS runs the tests."""

from palctl import procs


def test_parse_sc_state():
    assert procs._parse_sc_state("        STATE              : 4  RUNNING") == "RUNNING"
    assert procs._parse_sc_state("        STATE              : 1  STOPPED") == "STOPPED"
    assert procs._parse_sc_state("nope") == "UNKNOWN"


def test_parse_systemctl_state():
    assert procs._parse_systemctl_state("active\n") == "RUNNING"
    assert procs._parse_systemctl_state("inactive") == "STOPPED"
    assert procs._parse_systemctl_state("failed") == "STOPPED"
    assert procs._parse_systemctl_state("activating") == "START_PENDING"
    assert procs._parse_systemctl_state("deactivating") == "STOP_PENDING"
    assert procs._parse_systemctl_state("garbage") == "UNKNOWN"


def test_command_builders_match_platform():
    state = procs._state_command("PalServer")
    start = procs._action_command("start", "PalServer")
    if procs.IS_WINDOWS:
        assert state == ["sc.exe", "query", "PalServer"]
        assert start == ["sc.exe", "start", "PalServer"]
    else:
        assert state == ["systemctl", "is-active", "PalServer"]
        assert start == ["systemctl", "start", "PalServer"]


def test_pal_process_names_cover_both_platforms():
    assert "PalServer-Win64-Shipping.exe" in procs.PAL_PROCESS_NAMES
    assert "PalServer-Linux-Shipping" in procs.PAL_PROCESS_NAMES
