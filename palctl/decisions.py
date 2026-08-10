"""A record of what palctl decided, and why.

The gap this fills is the one that made the original "either this app or the
server is hanging up badly" report take days to pin down. palctl was reasoning
correctly at the time — declining to recover a server it believed was meant to
be stopped, holding off during an operation, throttling after too many restarts
— and had nowhere to say so. The event feed carries what *happened* (server up,
server down, backup finished); nothing carried what palctl chose not to do,
which is exactly the part an admin cannot infer from the outside.

So every decision the supervisor makes is recorded here with its reason, and
`/state` and the diagnostics bundle carry the last few. The rule worth keeping:
if an admin can reasonably ask "why isn't it doing anything", the answer should
already be in this log.

Consecutive identical decisions collapse into one entry with a count — the poll
loop runs every few seconds, and a five-hour outage should read as one line
saying "auto-restart on crash is off, 4,120 times", not 4,120 lines.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# Enough to cover a long outage's worth of *distinct* decisions without letting
# a pathological flip-flop grow without bound. Collapsing makes this go far.
MAX_ENTRIES = 200


@dataclass
class DecisionEntry:
    action: str
    why: str
    first_at: float
    last_at: float
    count: int = 1
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "why": self.why,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "count": self.count,
            **({"data": self.data} if self.data else {}),
        }


class DecisionLog:
    """A bounded, newest-last ring of decisions. Single-threaded by design —
    it's written from the daemon's event loop and read by request handlers on
    that same loop."""

    def __init__(self, maxlen: int = MAX_ENTRIES):
        self._entries: deque[DecisionEntry] = deque(maxlen=maxlen)

    def record(
        self, action: str, why: str, *, data: dict | None = None, now: float | None = None
    ) -> DecisionEntry:
        """Add a decision, or extend the previous one when it repeats.

        Repetition is judged on the action *and* the reason: "confirming over 2
        more polls" and "confirming over 1 more poll" are the same action but
        genuinely different states, and collapsing them would hide the
        countdown that explains the wait."""
        at = time.time() if now is None else now
        if self._entries:
            last = self._entries[-1]
            if last.action == action and last.why == why and last.data == (data or {}):
                last.count += 1
                last.last_at = at
                return last
        entry = DecisionEntry(
            action=action, why=why, first_at=at, last_at=at, data=data or {}
        )
        self._entries.append(entry)
        return entry

    def latest(self) -> DecisionEntry | None:
        return self._entries[-1] if self._entries else None

    def entries(self, limit: int = 25) -> list[dict]:
        """Newest first — what a dashboard or a support bundle wants to show at
        the top."""
        items = list(self._entries)[-limit:]
        return [e.as_dict() for e in reversed(items)]

    def __len__(self) -> int:
        return len(self._entries)


def summarize(entry: DecisionEntry | None) -> str:
    """One line for a human: the current reason, and how long it has held.
    Used for `palctl status` and the dashboard's 'why' strip."""
    if entry is None:
        return "no decisions recorded yet"
    held = max(0.0, entry.last_at - entry.first_at)
    if entry.count > 1 and held >= 60:
        return f"{entry.why} (for {int(held // 60)}m)"
    return entry.why
