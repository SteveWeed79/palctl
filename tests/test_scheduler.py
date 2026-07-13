from datetime import datetime

from palctl.config import Config
from palctl.events import EventBus
from palctl.scheduler import Scheduler


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
