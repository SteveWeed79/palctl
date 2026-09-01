"""Who asked for this?

Four surfaces can stop the server — the desktop GUI, the web dashboard, the CLI
and the Discord bot. The event feed recorded that a stop happened and never who
asked for it, so the first question after a surprise ("why did the server go
down?") had no answer inside palctl.

Attribution rides a ContextVar rather than a parameter threaded through every
call, because an admin's Stop travels daemon -> scheduler -> controller and
emits events at each layer.
"""

from __future__ import annotations

import asyncio
import json

from palctl.cli import fmt_events
from palctl.events import Event, EventBus, acting_as, current_actor, set_actor


def collect(bus: EventBus) -> list[Event]:
    seen: list[Event] = []

    async def handler(e: Event) -> None:
        seen.append(e)

    bus.on_any(handler)
    return seen


def test_an_event_is_unattributed_by_default():
    """Absence is meaningful: it means palctl decided this by itself."""
    e = Event("backup", "📦 Backup done")
    assert e.actor == ""
    assert e.via == ""


def test_emitting_inside_an_actor_block_stamps_the_event():
    bus = EventBus()
    seen = collect(bus)

    async def go():
        with acting_as("zoe", "discord"):
            await bus.emit(Event("restart", "🔁 Restarting"))

    asyncio.run(go())

    assert (seen[0].actor, seen[0].via) == ("zoe", "discord")


def test_attribution_does_not_leak_past_the_block():
    bus = EventBus()
    seen = collect(bus)

    async def go():
        with acting_as("zoe", "discord"):
            await bus.emit(Event("restart", "by a person"))
        await bus.emit(Event("watchdog", "by palctl itself"))

    asyncio.run(go())

    assert seen[1].actor == ""
    assert seen[1].via == ""


def test_an_event_that_names_its_own_actor_is_left_alone():
    bus = EventBus()
    seen = collect(bus)

    async def go():
        with acting_as("zoe", "discord"):
            await bus.emit(Event("restart", "relayed", actor="ana", via="api"))

    asyncio.run(go())

    assert (seen[0].actor, seen[0].via) == ("ana", "api")


def test_a_spawned_task_inherits_the_actor():
    """The property that makes this work: a /restart whose countdown ends ten
    minutes later is still recorded as that person's restart."""
    bus = EventBus()
    seen = collect(bus)

    async def later():
        await asyncio.sleep(0)
        await bus.emit(Event("restart", "✅ Server back up."))

    async def go():
        with acting_as("zoe", "discord"):
            task = asyncio.create_task(later())  # context copied at creation
        await task

    asyncio.run(go())

    assert (seen[0].actor, seen[0].via) == ("zoe", "discord")


def test_two_concurrent_admins_do_not_cross_attribution():
    bus = EventBus()
    seen = collect(bus)

    async def act(name: str, via: str) -> None:
        set_actor(name, via)
        await asyncio.sleep(0)
        await bus.emit(Event("restart", f"{name} acted"))

    async def go():
        await asyncio.gather(act("zoe", "discord"), act("ana", "cli"))

    asyncio.run(go())

    by = {e.message: (e.actor, e.via) for e in seen}
    assert by["zoe acted"] == ("zoe", "discord")
    assert by["ana acted"] == ("ana", "cli")


def test_set_actor_is_scoped_to_its_own_task():
    """set_actor has no block to reset, so isolation has to come from the task
    context — otherwise one request would attribute the next one's events."""

    async def go():
        async def one():
            set_actor("zoe", "discord")

        await asyncio.create_task(one())
        return current_actor()

    assert asyncio.run(go()) == ("", "")


# ---------------- persistence ----------------


def test_attribution_is_persisted_without_a_schema_migration(tmp_path):
    """It rides inside the existing `data` blob: the events table exists on
    every install, and a migration to record who pressed Stop is not worth the
    risk to an append-only table."""
    from palctl import events as events_mod

    # Passed explicitly: SessionStore binds DB_PATH as a default argument at
    # import time, so monkeypatching the module attribute is a no-op.
    store = events_mod.SessionStore(tmp_path / "sessions.db")
    try:
        store.log_event(Event("restart", "🔁", {"reason": "x"}, actor="zoe", via="cli"))
        rows = store._db.execute("SELECT data FROM events").fetchall()
    finally:
        store.close()

    data = json.loads(rows[0][0])
    assert data["actor"] == "zoe"
    assert data["via"] == "cli"
    assert data["reason"] == "x"  # the original payload survives


def test_an_unattributed_event_stores_no_actor_keys(tmp_path):
    from palctl import events as events_mod

    store = events_mod.SessionStore(tmp_path / "sessions.db")
    try:
        store.log_event(Event("watchdog", "restarted on memory", {"mb": 9000}))
        rows = store._db.execute("SELECT data FROM events").fetchall()
    finally:
        store.close()

    assert json.loads(rows[0][0]) == {"mb": 9000}


# ---------------- rendering ----------------


def test_the_cli_names_the_person_who_asked():
    line = fmt_events(
        [{"at": "2026-09-01T12:00:00", "kind": "restart", "message": "🔁 Restarting",
          "actor": "zoe", "via": "discord"}]
    )
    assert "— by zoe (discord)" in line


def test_the_cli_says_nothing_for_palctls_own_decisions():
    line = fmt_events(
        [{"at": "2026-09-01T12:00:00", "kind": "watchdog", "message": "memory limit"}]
    )
    assert "—" not in line


def test_a_surface_with_no_person_still_names_the_surface():
    line = fmt_events(
        [{"at": "2026-09-01T12:00:00", "kind": "backup", "message": "📦",
          "actor": "", "via": "web"}]
    )
    assert "— by web" in line
