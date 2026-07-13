import asyncio
from pathlib import Path

from palctl.api import Player
from palctl.events import Event, EventBus, PlayerTracker, SessionStore


def player(uid: str, name: str = "", level: int = 1) -> Player:
    return Player(
        name=name or uid,
        account_name=uid,
        player_id=uid,
        user_id=uid,
        ip="127.0.0.1",
        ping=20.0,
        location_x=0.0,
        location_y=0.0,
        level=level,
        building_count=0,
    )


def collect_bus() -> tuple[EventBus, list[Event]]:
    bus = EventBus()
    seen: list[Event] = []

    async def record(e: Event) -> None:
        seen.append(e)

    bus.on_any(record)
    return bus, seen


# ---------------- EventBus ----------------


def test_bus_broken_handler_does_not_stop_others():
    bus, seen = collect_bus()

    async def broken(e: Event) -> None:
        raise RuntimeError("boom")

    bus.on("join", broken)
    asyncio.run(bus.emit(Event("join", "hi")))
    assert len(seen) == 1  # the on_any handler still ran
    assert bus.recent() and bus.recent()[-1].message == "hi"


# ---------------- PlayerTracker ----------------


def test_first_poll_is_silent(tmp_path: Path):
    bus, seen = collect_bus()
    tracker = PlayerTracker(bus, SessionStore(tmp_path / "s.db"))

    asyncio.run(tracker.update([player("a"), player("b")]))
    assert seen == []  # players online at daemon start are not "joins"
    assert {p.user_id for p in tracker.online} == {"a", "b"}


def test_join_leave_levelup(tmp_path: Path):
    bus, seen = collect_bus()
    store = SessionStore(tmp_path / "s.db")
    tracker = PlayerTracker(bus, store)

    async def scenario():
        await tracker.update([player("a", level=5)])          # prime
        await tracker.update([player("a", level=5), player("b")])  # b joins
        await tracker.update([player("a", level=6), player("b")])  # a levels
        await tracker.update([player("b")])                    # a leaves

    asyncio.run(scenario())
    assert [e.kind for e in seen] == ["join", "levelup", "leave"]
    assert seen[0].data["user_id"] == "b"
    assert seen[1].data == {"name": "a", "user_id": "a", "from": 5, "to": 6}
    assert store.total_playtime_minutes("a") >= 0.0


def test_server_down_closes_sessions(tmp_path: Path):
    bus, seen = collect_bus()
    store = SessionStore(tmp_path / "s.db")
    tracker = PlayerTracker(bus, store)

    async def scenario():
        await tracker.update([player("a")])
        await tracker.handle_server_down()
        # Server comes back: first poll re-primes silently.
        await tracker.update([player("a")])

    asyncio.run(scenario())
    assert seen == []
    assert tracker.online[0].user_id == "a"


# ---------------- SessionStore ----------------


def test_playtime_accumulates(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    p = player("a", level=3)
    store.open_session(p)
    minutes = store.close_session("a", level=4)
    assert minutes >= 0.0
    assert store.total_playtime_minutes("a") == minutes


def test_close_session_without_open_returns_zero(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    assert store.close_session("ghost", level=1) == 0.0


def test_dangling_sessions_closed_on_restart(tmp_path: Path):
    db = tmp_path / "s.db"
    store = SessionStore(db)
    store.open_session(player("a"))
    # Daemon dies here without close_session. New daemon run:
    store2 = SessionStore(db)
    store2.open_session(player("a"))
    minutes = store2.close_session("a", level=2)
    # The close must hit the NEW session, not the dangling one.
    assert minutes >= 0.0
    rows = store2._db.execute(
        "SELECT COUNT(*) FROM sessions WHERE left_at IS NULL"
    ).fetchone()
    assert rows[0] == 0


def test_log_event_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    store.log_event(Event("watchdog", "msg", {"memory_mb": 12345.0}))
    row = store._db.execute("SELECT kind, message FROM events").fetchone()
    assert row == ("watchdog", "msg")


def test_metrics_roundtrip_survives_daemon_restart(tmp_path: Path):
    db = tmp_path / "s.db"
    store = SessionStore(db)
    for i in range(5):
        store.log_metrics(
            {"at": 1000.0 + i, "fps": 60, "frame_time": 16.6,
             "players": 2, "memory_mb": 8000.0 + i, "cpu": 40.0}
        )
    # A "new daemon" reads the same samples back, oldest first.
    got = SessionStore(db).recent_metrics(720)
    assert [s["at"] for s in got] == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]
    assert got[-1]["memory_mb"] == 8004.0


def test_metrics_limit_and_retention(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    store.log_metrics({"at": 100.0, "memory_mb": 1.0})
    # A sample 8 days later prunes the ancient one on the way in.
    later = 100.0 + 8 * 24 * 3600
    store.log_metrics({"at": later, "memory_mb": 2.0})
    got = store.recent_metrics(720)
    assert [s["at"] for s in got] == [later]
    # And LIMIT returns the newest n, still oldest-first.
    store.log_metrics({"at": later + 1, "memory_mb": 3.0})
    assert [s["memory_mb"] for s in store.recent_metrics(1)] == [3.0]
