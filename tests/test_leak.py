"""The leak forecaster decides when to say "the limit is coming" and, opt-in,
when to restart an empty server early. A wrong forecast either cries wolf or
restarts for no reason, so the estimator is pure and pinned here. No heavy
imports — this runs on the minimal-deps CI job too."""

from palctl.leak import fmt_minutes, time_to_limit_minutes


def growth(start_mb: float, mb_per_min: float, minutes: int, step_s: int = 60):
    """(epoch, memory) samples of a steady leak."""
    return [
        (1000.0 + i * step_s, start_mb + mb_per_min * (i * step_s / 60))
        for i in range(minutes * 60 // step_s + 1)
    ]


def test_steady_leak_predicts_the_crossing():
    # 8000 MB growing 50 MB/min toward a 12000 MB limit -> 80 minutes out,
    # minus the 30 minutes of samples already elapsed => ~50 remaining.
    ttl = time_to_limit_minutes(growth(8000, 50, 30), 12_000)
    assert ttl is not None
    assert 48 <= ttl <= 52


def test_flat_memory_gives_no_forecast():
    samples = [(1000.0 + i * 60, 9000.0) for i in range(31)]
    assert time_to_limit_minutes(samples, 12_000) is None


def test_falling_memory_gives_no_forecast():
    ttl = time_to_limit_minutes(growth(10_000, -20, 30), 12_000)
    assert ttl is None


def test_too_few_points_or_too_short_a_span():
    assert time_to_limit_minutes(growth(8000, 50, 5), 12_000) is None  # 6 min span
    few = growth(8000, 50, 30)[::10]  # long span but only 4 points
    assert time_to_limit_minutes(few, 12_000) is None


def test_already_over_the_limit_says_now():
    assert time_to_limit_minutes(growth(13_000, 10, 30), 12_000) == 0.0


def test_distant_crossings_are_noise_not_forecasts():
    # 1 MB/min from 8000 toward 12000 = ~66h away. Too far to trust.
    assert time_to_limit_minutes(growth(8000, 1, 30), 12_000) is None


def test_zero_memory_samples_are_ignored():
    # Polls where the process wasn't found record 0 MB; they must not drag
    # the fitted line to the floor.
    samples = growth(8000, 50, 30)
    with_gaps = [(t, 0.0) if i % 5 == 0 else (t, m) for i, (t, m) in enumerate(samples)]
    ttl = time_to_limit_minutes(with_gaps, 12_000)
    assert ttl is not None and 45 <= ttl <= 55


def test_fmt_minutes():
    assert fmt_minutes(45.4) == "~45m"
    assert fmt_minutes(125) == "~2h 05m"
    assert fmt_minutes(0.2) == "~0m"
