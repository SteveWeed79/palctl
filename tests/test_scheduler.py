from datetime import datetime

import pytest

from palctl.config import Config
from palctl.events import EventBus
from palctl.scheduler import Scheduler, backup_interval_hours, next_daily


@pytest.mark.parametrize(
    "raw, expected",
    [
        (6, 6),    # a normal, more-frequent-than-daily choice is untouched
        (1, 1),    # hourly is fine
        (24, 24),  # exactly daily
        (25, 24),  # anything over a day is pulled back to the daily floor
        (48, 24),  # a stale pre-cap config, too
        (0, 0),    # the explicit "off" sentinel is preserved
        (-3, -3),  # negatives likewise
    ],
)
def test_backup_interval_hours_enforces_daily_floor(raw, expected):
    assert backup_interval_hours(raw) == expected


def test_next_daily_later_today():
    t = next_daily(datetime(2026, 1, 1, 3, 0), "06:00")
    assert (t.month, t.day, t.hour, t.minute) == (1, 1, 6, 0)


def test_next_daily_rolls_to_tomorrow():
    t = next_daily(datetime(2026, 1, 1, 7, 0), "06:00")
    assert (t.day, t.hour) == (2, 6)


def test_next_daily_falls_back_on_garbage():
    t = next_daily(datetime(2026, 1, 1, 3, 0), "not-a-time", fallback_hour=5)
    assert t.hour == 5


def make(restart_at: str) -> Scheduler:
    cfg = Config()
    cfg.schedule.daily_restart_at = restart_at
    return Scheduler(cfg, api=None, bus=EventBus())  # type: ignore[arg-type]


def test_next_restart_is_in_the_future():
    target = make("06:00")._next_restart()
    assert target > datetime.now()
    assert (target.hour, target.minute) == (6, 0)


def test_next_restart_survives_garbage():
    for bad in ("garbage", "25:99", "", "6:xx"):
        target = make(bad)._next_restart()
        assert target > datetime.now()  # falls back instead of raising
