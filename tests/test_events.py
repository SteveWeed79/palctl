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


def test_corrupt_sessions_db_is_quarantined_not_fatal(tmp_path: Path):
    # A corrupt sessions.db must not crash-loop the daemon under NSSM — same
    # policy as a corrupt config.json: set it aside and start fresh.
    db = tmp_path / "sessions.db"
    db.write_bytes(b"this is not a sqlite database, it is garbage")

    store = SessionStore(db)  # must not raise
    store.log_metrics({"at": 1.0, "memory_mb": 1.0})  # and must be usable

    assert (tmp_path / "sessions.db.broken").exists()  # evidence kept
    assert [s["at"] for s in store.recent_metrics(10)] == [1.0]


def test_unrecoverable_sessions_db_falls_back_to_memory(tmp_path: Path, monkeypatch):
    # If even the recreated file can't be opened (e.g. the disk is full), the
    # daemon still runs — history just doesn't persist.
    import palctl.events as ev

    db = tmp_path / "sessions.db"
    db.write_bytes(b"garbage")

    real_connect = ev.sqlite3.connect

    def connect_fails_for_files(target, **kw):
        if target != ":memory:":
            raise ev.sqlite3.OperationalError("database or disk is full")
        return real_connect(target, **kw)

    monkeypatch.setattr(ev.sqlite3, "connect", connect_fails_for_files)
    store = SessionStore(db)  # must not raise
    store.log_metrics({"at": 2.0, "memory_mb": 1.0})
    assert [s["at"] for s in store.recent_metrics(10)] == [2.0]


# ---------------- top_playtime (leaderboard source) ----------------

from datetime import UTC, datetime, timedelta  # noqa: E402


def _insert_closed_session(store: SessionStore, uid: str, name: str, minutes: float,
                           base: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> None:
    joined = base
    left = base + timedelta(minutes=minutes)
    store._db.execute(
        "INSERT INTO sessions (user_id, name, joined_at, left_at) VALUES (?,?,?,?)",
        (uid, name, joined.isoformat(), left.isoformat()),
    )
    store._db.commit()


def test_top_playtime_ranks_by_total_and_uses_latest_name(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    _insert_closed_session(store, "u1", "Alice", 30)
    _insert_closed_session(store, "u1", "AliceRenamed", 90)  # same account, later name
    _insert_closed_session(store, "u2", "Bob", 200)
    top = store.top_playtime(10)
    assert top[0] == ("Bob", 200.0)
    assert top[1] == ("AliceRenamed", 120.0)  # 30 + 90, most-recent name wins


def test_top_playtime_empty_and_limit(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    assert store.top_playtime() == []
    _insert_closed_session(store, "a", "A", 10)
    _insert_closed_session(store, "b", "B", 20)
    assert [name for name, _ in store.top_playtime(1)] == ["B"]


def test_top_playtime_skips_malformed_timestamps(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    store._db.execute(
        "INSERT INTO sessions (user_id, name, joined_at, left_at) VALUES (?,?,?,?)",
        ("x", "X", "not-a-date", "also-bad"),
    )
    store._db.commit()
    _insert_closed_session(store, "y", "Y", 15)
    assert store.top_playtime() == [("Y", 15.0)]  # the bad row is skipped, not fatal


# ---------------- offline lookup helpers (resolve / last_seen / recent) ----------------


def test_resolve_user_id_is_case_insensitive_and_takes_latest(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    _insert_closed_session(store, "u1", "Ghost", 10)
    _insert_closed_session(store, "u2", "Ghost", 10)  # a later account reused the name
    assert store.resolve_user_id("ghost") == "u2"  # most recent wins, case-insensitive
    assert store.resolve_user_id("nobody") is None


def test_last_seen_returns_most_recent_closed_session(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    _insert_closed_session(store, "u1", "Alice", 30, base=datetime(2026, 1, 1, tzinfo=UTC))
    _insert_closed_session(store, "u1", "Alice", 30, base=datetime(2026, 2, 2, tzinfo=UTC))
    name, left = store.last_seen("u1")
    assert name == "Alice"
    assert left.startswith("2026-02-02")
    assert store.last_seen("ghost") is None


def test_recent_player_names_dedups_keeping_most_recent_order(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    _insert_closed_session(store, "u1", "Alice", 5)
    _insert_closed_session(store, "u2", "Bob", 5)
    _insert_closed_session(store, "u1", "Alice", 5)  # Alice again, later
    # newest-first, each name once: Alice (its latest row) before Bob
    assert store.recent_player_names() == ["Alice", "Bob"]
