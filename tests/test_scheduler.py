from datetime import datetime

from palctl.config import Config
from palctl.events import EventBus
from palctl.scheduler import Scheduler, next_daily


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
