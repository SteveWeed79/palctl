"""Putting an empty server away, and — much more important — not putting away
one that isn't.

Every branch here fails safe toward *running*. A half-working auto-pause is
indistinguishable from a crashed server, so the tests are mostly about the
cases where palctl must decline to act.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from palctl.autopause import (
    MIN_IDLE_SECONDS,
    WAKE_GRACE_SECONDS,
    Action,
    KnockListener,
    Observation,
    decide,
)


def obs(**kw) -> Observation:
    """A server that is running, empty, and has been for long enough — i.e.
    the one case where pausing is correct. Each test breaks one thing."""
    base = dict(
        enabled=True,
        paused=False,
        knocked=False,
        players=0,
        alive=True,
        service_state="RUNNING",
        operation=None,
        desired_running=True,
        empty_seconds=MIN_IDLE_SECONDS + 1,
    )
    base.update(kw)
    return Observation(**base)


def test_an_idle_server_is_put_away():
    d = decide(obs())
    assert d.action is Action.PAUSE
    assert "minutes" in d.why


# ---------------- the refusals ----------------


def test_switched_off_means_never():
    assert decide(obs(enabled=False)).action is Action.HOLD


def test_a_deliberately_stopped_server_is_left_alone():
    """There is nothing to save and nothing to gain, and waking it would undo
    an admin's Stop."""
    assert decide(obs(desired_running=False)).action is Action.HOLD


def test_a_deliberately_stopped_server_is_never_woken_by_a_knock():
    """The important half: a stray packet must not restart a server somebody
    turned off on purpose."""
    d = decide(obs(desired_running=False, paused=True, knocked=True))
    assert d.action is Action.HOLD


def test_players_online_hold_it_open():
    d = decide(obs(players=3))
    assert d.action is Action.HOLD
    assert "3 player" in d.why


def test_an_unknown_player_count_is_not_an_empty_server():
    """"Nobody is on" and "I cannot tell" lead opposite ways, so they must not
    be the same value."""
    d = decide(obs(players=None))
    assert d.action is Action.HOLD
    assert "can't tell" in d.why


def test_a_server_whose_api_is_silent_is_the_watchdogs_business():
    """Pausing here would hide a sick server behind a deliberate-looking stop,
    where auto-recovery would never look at it again."""
    d = decide(obs(alive=False))
    assert d.action is Action.HOLD
    assert "isn't answering" in d.why


@pytest.mark.parametrize("state", ["STOPPED", "START_PENDING", "UNKNOWN", ""])
def test_only_a_running_service_is_a_candidate(state):
    assert decide(obs(service_state=state)).action is Action.HOLD


@pytest.mark.parametrize("op", ["backup", "update", "restore", "restart"])
def test_never_across_another_operation(op):
    """Stopping the server underneath a backup is how a world gets copied
    mid-write."""
    d = decide(obs(operation=op))
    assert d.action is Action.HOLD
    assert op in d.why


def test_a_briefly_empty_server_is_not_idle():
    """Two friends between sessions must not cost a minute of restarting."""
    d = decide(obs(empty_seconds=30))
    assert d.action is Action.HOLD
    assert "to go" in d.why


def test_the_idle_threshold_is_configurable():
    assert decide(obs(empty_seconds=120, idle_after_seconds=60)).action is Action.PAUSE
    assert decide(obs(empty_seconds=30, idle_after_seconds=60)).action is Action.HOLD


def test_a_freshly_woken_server_gets_time_for_players_to_load_in():
    """The player whose connection woke it takes tens of seconds to arrive.
    Without this the server puts itself away while they are still loading."""
    d = decide(obs(since_wake_seconds=10))
    assert d.action is Action.HOLD
    assert "recently woken" in d.why


def test_the_grace_period_does_expire():
    assert decide(obs(since_wake_seconds=WAKE_GRACE_SECONDS + 1)).action is Action.PAUSE


# ---------------- waking ----------------


def test_a_knock_wakes_a_sleeping_server():
    d = decide(obs(paused=True, knocked=True))
    assert d.action is Action.WAKE
    assert "connect" in d.why


def test_a_sleeping_server_with_no_knock_keeps_sleeping():
    d = decide(obs(paused=True, knocked=False))
    assert d.action is Action.HOLD
    assert "asleep" in d.why


def test_a_sleeping_server_is_never_paused_again():
    """Belt and braces: the paused branch is checked before every condition
    that could otherwise produce a second PAUSE."""
    for extra in ({"service_state": "STOPPED"}, {"alive": False}, {"players": None}):
        assert decide(obs(paused=True, **extra)).action is Action.HOLD


# ---------------- the knock listener ----------------


def test_the_listener_takes_the_port_and_reports_a_knock():
    """The whole wake mechanism, and only possible because palctl stops the
    server rather than suspending it — a suspended process would still hold
    this port."""

    async def go():
        listener = KnockListener()
        # Port 0 would be assigned, so take a real free one first.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        assert await listener.start("127.0.0.1", port)
        assert listener.listening
        assert not listener.knocked

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"hello", ("127.0.0.1", port))
        sender.close()
        for _ in range(50):
            if listener.knocked:
                break
            await asyncio.sleep(0.02)

        knocked = listener.knocked
        await listener.stop()
        return knocked, listener.listening

    knocked, still_listening = asyncio.run(go())
    assert knocked
    assert not still_listening


def test_a_port_it_cannot_take_is_reported_not_raised():
    """The caller must treat this as a reason NOT to pause: a server put away
    with no way to wake it is a server nobody can reach."""

    async def go():
        held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]

        listener = KnockListener()
        ok = await listener.start("127.0.0.1", port)
        held.close()
        return ok, listener.error, listener.listening

    ok, error, listening = asyncio.run(go())
    assert not ok
    assert error
    assert not listening


def test_stopping_releases_the_port_so_the_server_can_have_it_back():
    """If palctl kept the port, the woken server would find it taken and exit —
    turning a sleeping server into a dead one."""

    async def go():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        listener = KnockListener()
        await listener.start("127.0.0.1", port)
        released = await listener.stop("127.0.0.1", port)

        # And it really is bindable now, not merely reported as such.
        after = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            after.bind(("127.0.0.1", port))
            bound = True
        except OSError:
            bound = False
        finally:
            after.close()
        return released and bound

    assert asyncio.run(go())


def test_starting_twice_is_harmless():
    async def go():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        listener = KnockListener()
        first = await listener.start("127.0.0.1", port)
        second = await listener.start("127.0.0.1", port)
        await listener.stop()
        return first, second

    assert asyncio.run(go()) == (True, True)
