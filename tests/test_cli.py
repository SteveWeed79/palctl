"""The CLI's formatting and name→user_id resolution are the parts that can
quietly lie to an admin (or kick the wrong player), so they're pinned here.
The network layer is a thin httpx wrapper exercised for its error translation
only. Skips cleanly on the minimal-deps CI job."""

import pytest

pytest.importorskip("httpx")
pytest.importorskip("keyring")

from palctl.cli import (  # noqa: E402  (after importorskip)
    find_players,
    fmt_backups,
    fmt_players,
    fmt_status,
)

STATE = {
    "service": "RUNNING",
    "alive": True,
    "restarting": False,
    "operation": None,
    "metrics": {
        "server_fps": 58,
        "current_players": 3,
        "server_frame_time": 17.2,
        "max_players": 32,
        "uptime": 19_020,  # 5h 17m
        "base_camps": 17,
        "days": 42,
    },
    "process": {"pid": 1, "memory_mb": 9412.3, "cpu_percent": 63.4,
                "threads": 40, "uptime_seconds": 19_000},
    "players": [
        {"name": "Zoe", "user_id": "steam_1", "level": 31, "ping": 24.6,
         "building_count": 120},
        {"name": "Max", "user_id": "steam_2", "level": 12, "ping": 101.0,
         "building_count": 8},
    ],
}


def test_fmt_status_reads_like_a_status():
    out = fmt_status(STATE)
    assert "RUNNING" in out and "REST API answering" in out
    assert "3/32" in out
    assert "58" in out and "17.2 ms" in out
    assert "5h 17m" in out
    assert "day 42" in out and "17 base camps" in out
    assert "9,412 MB" in out
    assert "operation" not in out  # nothing in flight, no noise


def test_fmt_status_shows_the_operation_and_survives_a_dead_server():
    out = fmt_status({"service": "STOPPED", "alive": False, "operation": "update",
                      "metrics": None, "process": None})
    assert "STOPPED" in out and "not answering" in out
    assert "update in progress" in out


def test_fmt_players_table_and_empty():
    out = fmt_players(STATE["players"])
    assert "Zoe" in out and "31" in out and "25ms" in out
    assert fmt_players([]) == "Nobody online."


def test_fmt_backups():
    out = fmt_backups([{"name": "2026-01-01_00-00-00-manual", "size_mb": 123.4}])
    assert "2026-01-01_00-00-00-manual" in out and "123 MB" in out
    assert fmt_backups([]) == "No backups yet."


def test_find_players_is_case_insensitive_and_exact():
    assert [p["user_id"] for p in find_players(STATE["players"], "zoe")] == ["steam_1"]
    assert [p["user_id"] for p in find_players(STATE["players"], "ZOE")] == ["steam_1"]
    assert find_players(STATE["players"], "Zo") == []  # no prefix guessing
    assert find_players([], "Zoe") == []


def test_find_players_surfaces_duplicates():
    # Palworld names aren't unique. Moderation must see BOTH matches and
    # refuse, not silently kick whoever the API listed first.
    dupes = STATE["players"] + [
        {"name": "zoe", "user_id": "steam_9", "level": 5, "ping": 30.0,
         "building_count": 1},
    ]
    assert [p["user_id"] for p in find_players(dupes, "Zoe")] == ["steam_1", "steam_9"]
