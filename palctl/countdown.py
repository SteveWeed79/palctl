"""
The wait before palctl takes the server down — and the two ways out of it.

Every operation that disconnects players used to own its own timing. The
scheduled/admin restart counted down for a hard-coded ten minutes with exactly
one escape hatch (Discord's `/cancel`, which most installs don't have because
the bot is off by default), and a restore didn't count down at all: it stopped
the server the instant the button was clicked. So the admin had ten minutes of
nothing they could do, or no window at all — and no surface in between.

This module is the one countdown, shared by both, with three properties the old
code didn't have:

  * **Its length is a number, not a constant.** `marks_for()` derives the
    announce schedule from whatever total it's given, so 30 seconds and 20
    minutes both announce sensibly instead of only 600 doing so.
  * **cancel() and skip() are different verbs.** Cancel calls the operation
    off; the server stays up. Skip says "stop waiting, do it now". Conflating
    them is why "I want this to happen sooner" had no answer but "wait".
  * **It can be read while it runs.** `state()` is what `/state` publishes, so
    the dashboard, GUI, CLI and bot can all show the same live clock and offer
    the same two buttons, instead of the countdown being invisible to
    everything except the Discord channel it announced in.

Nothing here touches the server. The caller owns the operation and decides what
"go" means; this owns only the waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

# The marks a countdown announces at, longest first. Only the ones shorter than
# the countdown itself are used, so the schedule scales down with the total
# instead of a 30-second countdown announcing "5 minutes".
DEFAULT_MARKS = (1800, 900, 600, 300, 120, 60, 30, 10)

# What a countdown collapses to when there is nobody to warn. Not zero: a few
# seconds still lets the world flush and gives an admin who just realised their
# mistake something to hit Cancel on.
EMPTY_SERVER_SECONDS = 10

# Longest countdown palctl will honour. A hand-edited config asking for a
# 30-hour countdown would hold the operation lock — and so block the watchdog,
# backups and updates — for the rest of the day.
MAX_SECONDS = 3600


def clamp_seconds(value: int) -> int:
    """A countdown length from config or an API call, made safe. Negative reads
    as 0 ("no countdown"), and nothing may exceed MAX_SECONDS."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_SECONDS, n))


def marks_for(total: int, marks: tuple[int, ...] = DEFAULT_MARKS) -> tuple[int, ...]:
    """The seconds-remaining marks a countdown of `total` announces at.

    The total itself is always the first mark (the opening "restarting in N"),
    followed by every standard mark strictly below it. `total <= 0` announces
    nothing, because there is no wait to describe.
    """
    if total <= 0:
        return ()
    return (total, *(m for m in marks if m < total))


def humanize(seconds: int) -> str:
    """'10 minutes' / '90 seconds' — the countdown mark as players read it."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{seconds} second" + ("s" if seconds != 1 else "")


class Countdown:
    """One in-flight countdown, cancellable and skippable while it runs.

    `announce` is the in-game message callback (best-effort — a server that
    isn't answering must not abort the countdown), `notify` is the operator
    event callback. Both are awaited; either may be None.
    """

    def __init__(
        self,
        kind: str,
        reason: str,
        total: int,
        *,
        announce_as: str | None = None,
        announce: Callable[[str], Awaitable[None]] | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.kind = kind  # "restart" | "restore" — what the wait is for
        self.reason = reason
        # What players are told, when that isn't what the operator is told. A
        # restore's operator reason names the backup directory; in-game that is
        # a timestamped folder name nobody outside the server room can read.
        self.announce_as = announce_as or reason
        self.total = clamp_seconds(total)
        self._announce = announce
        self._notify = notify
        # One event, one verdict. Both endings interrupt the same wait, so a
        # single wake-up is enough and there's no window where two events are
        # set and the winner is whichever branch is checked first.
        self._wake = asyncio.Event()
        self._verdict = ""  # "" | "cancelled" | "skipped"
        self._ends_at: float | None = None  # loop time, set when run() starts
        self._finished = False

    # ---------- the two ways out ----------

    def cancel(self) -> bool:
        """Call the whole operation off. True if this countdown accepted it."""
        return self._end("cancelled")

    def skip(self) -> bool:
        """Stop waiting and go now. True if this countdown accepted it."""
        return self._end("skipped")

    def _end(self, verdict: str) -> bool:
        # First verdict wins: a cancel that lands microseconds after a skip must
        # not un-cancel it, and neither may touch a countdown that already ran
        # out — past that point the caller is mid-stop and there is nothing left
        # to interrupt.
        if self._finished or self._verdict:
            return False
        self._verdict = verdict
        self._wake.set()
        return True

    # ---------- what the UIs read ----------

    @property
    def finished(self) -> bool:
        return self._finished

    def remaining(self) -> float:
        """Seconds left, from the event loop's own clock. 0 once there is
        nothing left to wait for — including a countdown that was skipped,
        whose nominal end time is still in the future but whose wait is over."""
        if self._verdict or self._finished:
            return 0.0
        if self._ends_at is None:
            return float(self.total)
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            # Read from outside the loop that owns the clock: the deadline is
            # in that loop's time base and means nothing here.
            return 0.0
        return max(0.0, self._ends_at - now)

    def state(self) -> dict:
        """The countdown as `/state` publishes it — enough for any client to
        draw a live clock and decide whether Cancel/Now are worth offering."""
        return {
            "kind": self.kind,
            "reason": self.reason,
            "total_seconds": self.total,
            "seconds_remaining": round(self.remaining(), 1),
            # False the moment the wait is over: the operation is committed and
            # a button that claims otherwise is lying.
            "cancellable": not (self._finished or bool(self._verdict)),
            "verdict": self._verdict,
        }

    # ---------- running it ----------

    async def run(self) -> bool:
        """Wait the countdown out, announcing as it goes.

        Returns True if the operation should proceed (the clock ran out, or an
        admin skipped ahead) and False if it was cancelled. Always sets
        `finished`, so a cancel arriving after the last mark is refused rather
        than silently ignored by a caller already taking the server down.
        """
        loop = asyncio.get_running_loop()
        self._ends_at = loop.time() + self.total
        try:
            return await self._run()
        finally:
            self._finished = True

    async def _run(self) -> bool:
        prev = self.total
        for mark in marks_for(self.total):
            verdict = await self._wait(prev - mark)
            if verdict:
                return await self._early(verdict)
            prev = mark
            await self._say(f"{self.announce_as} in {humanize(mark)}.")

        verdict = await self._wait(prev)
        if verdict:
            return await self._early(verdict)
        return True

    async def _early(self, verdict: str) -> bool:
        if verdict == "cancelled":
            await self._tell(
                f"🚫 Cancelled — '{self.reason}' called off. The server stays up."
            )
            return False
        await self._tell(f"⏩ Skipped the countdown — {self.reason.lower()} now.")
        await self._say(f"{self.announce_as} now.")
        return True

    async def _wait(self, delay: float) -> str:
        """Sleep `delay`, returning early with the verdict if one lands."""
        if self._verdict:
            return self._verdict
        if delay > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
        return self._verdict

    async def _say(self, message: str) -> None:
        """In-game announcement. Best-effort by design: a server whose REST API
        has stopped answering is exactly when a restart matters most, and an
        exception here must never abort the countdown that fixes it."""
        if self._announce is None:
            return
        with contextlib.suppress(Exception):
            await self._announce(message)

    async def _tell(self, message: str) -> None:
        if self._notify is None:
            return
        with contextlib.suppress(Exception):
            await self._notify(message)
