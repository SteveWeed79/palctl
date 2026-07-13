"""
Memory-leak forecasting.

The watchdog reacts at the threshold; this predicts it. A least-squares line
through the recent memory samples answers one question: *on the current pace,
how many minutes until the limit?* That enables two things the reactive
watchdog can't do:

  * warn the admin while there's still time to pick a good moment, and
  * (opt-in) restart early while the server happens to be empty, instead of
    at the threshold two hours later with six people mid-boss.

Deliberately conservative: no forecast without enough samples spanning enough
time, no forecast if memory isn't actually growing, and nothing beyond a day
out (that far, the line is noise).
"""

from __future__ import annotations

from collections.abc import Sequence

MAX_FORECAST_MINUTES = 24 * 60.0


def time_to_limit_minutes(
    samples: Sequence[tuple[float, float]],
    limit_mb: float,
    *,
    min_points: int = 12,
    min_span_seconds: float = 900.0,
) -> float | None:
    """
    Minutes until `limit_mb` on the current trend, from (epoch_seconds,
    memory_mb) samples. None = no confident forecast (too little data, too
    short a span, memory flat or falling, or the crossing is >24h out).
    0.0 = the fitted line says we're already there.
    """
    usable = [(t, m) for t, m in samples if m > 0]
    if len(usable) < min_points:
        return None

    t0 = usable[0][0]
    span = usable[-1][0] - t0
    if span < min_span_seconds:
        return None

    xs = [t - t0 for t, _ in usable]
    ys = [m for _, m in usable]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None

    slope = (n * sxy - sx * sy) / denom  # MB per second
    intercept = (sy - slope * sx) / n

    # Judge from the fitted line, not the last raw sample — a lone spike
    # shouldn't say "we're there" any more than a lone dip should say "fine".
    current = intercept + slope * xs[-1]
    if current >= limit_mb:
        return 0.0
    if slope <= 0:
        return None  # not leaking right now; nothing to forecast

    minutes = (limit_mb - current) / slope / 60.0
    return None if minutes > MAX_FORECAST_MINUTES else minutes


def fmt_minutes(minutes: float) -> str:
    """'~2h 05m' / '~45m' — for the notification."""
    m = max(0, round(minutes))
    if m >= 60:
        return f"~{m // 60}h {m % 60:02d}m"
    return f"~{m}m"
