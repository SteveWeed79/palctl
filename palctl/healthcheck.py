"""
The health-check probe the scheduled task runs (`palctl-daemon health-check`).

One probe is not a verdict: a daemon mid-restart, or a box waking from sleep,
briefly fails /healthz without being wedged. So failures are counted across
runs in a small state file, and only `threshold` consecutive failures trigger
a heal — the same confirm-before-acting shape the crash watchdog uses for the
game server. A single healthy probe resets the streak.

The decision is pure (`decide`) so the whole policy is testable; the probe and
the state file are the only I/O here. The heal itself lives in daemon.py next
to the other process-control helpers.
"""

from __future__ import annotations

import json

from .config import config_dir

# Consecutive failed probes before the daemon is restarted. At the task's
# 5-minute cadence this is ~15 minutes of confirmed wedge — slow enough to
# never fight a deliberate restart, fast enough that an unattended box heals
# the same evening, not next month.
DEFAULT_THRESHOLD = 3

_STATE_PATH = config_dir() / "health_state.json"


def decide(*, healthy: bool, failures: int, threshold: int) -> tuple[str, int]:
    """(action, new_failure_count). Actions:
    'ok'   — healthy; streak resets.
    'wait' — unhealthy, but not for long enough yet; count it.
    'heal' — threshold consecutive failures; restart the daemon and reset
             (reset matters: if the heal itself fails, the NEXT threshold
             failures trigger another attempt instead of healing every run)."""
    if healthy:
        return "ok", 0
    failures += 1
    if failures < max(1, threshold):
        return "wait", failures
    return "heal", 0


def load_failures() -> int:
    try:
        return int(json.loads(_STATE_PATH.read_text(encoding="utf-8"))["failures"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0  # no/corrupt state = fresh streak


def save_failures(n: int) -> None:
    try:
        _STATE_PATH.write_text(json.dumps({"failures": n}), encoding="utf-8")
    except OSError:
        pass  # best effort; worst case the streak restarts from 0


def probe(timeout: float = 5.0) -> bool:
    """Is the daemon actually healthy — not merely accepting connections?
    /healthz answers 200 only while the poll loop is completing cycles; a
    wedged daemon (the case this whole mechanism exists for) accepts TCP and
    then 503s or hangs. Any error, timeout, or non-200 is unhealthy."""
    import httpx

    from .client import DAEMON_PORT

    try:
        r = httpx.get(f"http://127.0.0.1:{DAEMON_PORT}/healthz", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False
