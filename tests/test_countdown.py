"""The wait before palctl takes the server down.

Two things are pinned here. The pure half — how a countdown of N seconds turns
into announce marks — because it is what makes the length a setting instead of
a constant. And the interruptible half, because "cancel" and "skip" are
different verbs with different outcomes, and a bug that conflated them would
either restart a server the admin just saved, or leave one up that they wanted
down.
"""

import asyncio

import pytest

from palctl import countdown
from palctl.countdown import Countdown


def _run(coro):
    return asyncio.run(coro)


# ---------------- marks ----------------


def test_marks_open_with_the_total_then_step_down():
    # The classic ten minutes: the opening announcement, then every standard
    # mark below it. The first mark being the total is what makes the loop's
    # first wait zero-length.
    assert countdown.marks_for(600) == (600, 300, 120, 60, 30, 10)


def test_marks_scale_down_with_a_short_countdown():
    # The bug a fixed tuple has: a 45-second countdown must not announce
    # "5 minutes" first.
    assert countdown.marks_for(45) == (45, 30, 10)
    assert countdown.marks_for(10) == (10,)


def test_marks_scale_up_without_repeating_the_total():
    assert countdown.marks_for(1800) == (1800, 900, 600, 300, 120, 60, 30, 10)


@pytest.mark.parametrize("total", [0, -1, -600])
def test_no_marks_for_a_zero_or_negative_countdown(total):
    assert countdown.marks_for(total) == ()


def test_marks_sum_to_the_total():
    # The marks are the schedule the runner sleeps through, so the gaps between
    # them plus the final leg must add up to exactly the requested wait.
    for total in (10, 45, 90, 600, 1800):
        marks = countdown.marks_for(total)
        gaps = [a - b for a, b in zip(marks, marks[1:], strict=False)]
        assert sum(gaps) + marks[-1] == total


# ---------------- clamping ----------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (600, 600),
        (0, 0),
        (-5, 0),  # a negative countdown is "none", never a negative sleep
        (999_999, countdown.MAX_SECONDS),  # a hand-edited config can't wedge the lock
        ("nonsense", 0),
        (None, 0),
    ],
)
def test_clamp_seconds(raw, want):
    assert countdown.clamp_seconds(raw) == want


@pytest.mark.parametrize(
    ("seconds", "want"),
    [(600, "10 minutes"), (60, "1 minute"), (90, "90 seconds"), (10, "10 seconds"),
     (1, "1 second")],
)
def test_humanize(seconds, want):
    assert countdown.humanize(seconds) == want


# ---------------- running, cancelling, skipping ----------------


def _instrumented(total: int, kind: str = "restart", reason: str = "Test restart"):
    said: list[str] = []
    told: list[str] = []

    async def announce(m):
        said.append(m)

    async def notify(m):
        told.append(m)

    return Countdown(kind, reason, total, announce=announce, notify=notify), said, told


def test_a_zero_countdown_goes_straight_through():
    cd, said, _told = _instrumented(0)
    assert _run(cd.run()) is True
    assert said == []  # nothing to announce; there was no wait


def test_a_countdown_announces_every_mark_and_then_proceeds():
    cd, said, _told = _instrumented(30)

    async def go():
        # Skip immediately so the test doesn't sleep 30 seconds; the opening
        # announcement has already been made by then.
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        cd.skip()
        return await task

    assert _run(go()) is True
    assert said and said[0] == "Test restart in 30 seconds."


def test_cancel_stops_the_operation():
    cd, _said, told = _instrumented(600)

    async def go():
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        assert cd.cancel() is True
        return await task

    assert _run(go()) is False
    assert any("cancelled" in m.lower() for m in told)


def test_skip_ends_the_wait_but_the_operation_still_runs():
    cd, said, told = _instrumented(600)

    async def go():
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        assert cd.skip() is True
        return await task

    assert _run(go()) is True
    assert any("skipped" in m.lower() for m in told)
    # Players hear about it too — the last thing announced is "now", not a
    # countdown mark that will never arrive.
    assert said[-1] == "Test restart now."


def test_the_first_verdict_wins():
    # A cancel landing microseconds after a skip must not un-skip it, and vice
    # versa: the operation is already committed either way.
    cd, _said, _told = _instrumented(600)

    async def go():
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        assert cd.skip() is True
        assert cd.cancel() is False  # refused — too late to change the answer
        return await task

    assert _run(go()) is True


def test_a_finished_countdown_refuses_both():
    cd, _said, _told = _instrumented(0)
    assert _run(cd.run()) is True
    assert cd.finished is True
    assert cd.cancel() is False
    assert cd.skip() is False


def test_state_reports_a_live_clock_and_goes_uncancellable_when_it_ends():
    cd, _said, _told = _instrumented(120, kind="restore", reason="Restoring backup X")

    async def go():
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        live = cd.state()
        cd.skip()
        await task
        return live, cd.state()

    live, done = _run(go())
    assert live["kind"] == "restore"
    assert live["reason"] == "Restoring backup X"
    assert live["total_seconds"] == 120
    assert 0 < live["seconds_remaining"] <= 120
    assert live["cancellable"] is True
    # Past the wait there is nothing left to interrupt; a UI that still offered
    # the buttons would be offering something that can only answer "too late".
    assert done["cancellable"] is False
    assert done["verdict"] == "skipped"


def test_an_announce_that_raises_does_not_abort_the_countdown():
    # A server whose REST API has stopped answering is exactly when a restart
    # matters most. Announcements are best-effort; the restart is not.
    async def explodes(_m):
        raise RuntimeError("REST API not answering")

    cd = Countdown("restart", "Test restart", 30, announce=explodes)

    async def go():
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        cd.skip()
        return await task

    assert _run(go()) is True


def test_players_can_be_told_something_different_from_the_operator():
    # A restore's operator wording names the backup directory. In-game that is
    # a timestamped folder name that means nothing to a player.
    cd, said, told = _instrumented(30, kind="restore", reason="Restoring backup 2026-01-01_x")
    cd.announce_as = "Server restart to restore a backup"

    async def go():
        task = asyncio.create_task(cd.run())
        await asyncio.sleep(0)
        cd.skip()
        return await task

    _run(go())
    assert said[0] == "Server restart to restore a backup in 30 seconds."
    # ...while the operator's record still says which backup.
    assert cd.state()["reason"] == "Restoring backup 2026-01-01_x"
    assert any("2026-01-01_x" in m for m in told)
