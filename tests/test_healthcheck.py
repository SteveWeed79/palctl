"""The health-check policy: one blip never heals, a confirmed streak does,
and the streak survives across runs (each run is a fresh process)."""

from palctl import healthcheck


def test_decide_matrix():
    # Healthy always resets — a daemon that recovered owes nothing.
    assert healthcheck.decide(healthy=True, failures=2, threshold=3) == ("ok", 0)
    # Failures accumulate below the threshold.
    assert healthcheck.decide(healthy=False, failures=0, threshold=3) == ("wait", 1)
    assert healthcheck.decide(healthy=False, failures=1, threshold=3) == ("wait", 2)
    # The threshold-th consecutive failure heals AND resets — so a heal that
    # doesn't take needs another full streak, not a restart every 5 minutes.
    assert healthcheck.decide(healthy=False, failures=2, threshold=3) == ("heal", 0)
    # A hand-edited threshold of 0/negative behaves like 1, never divide-by-zero
    # or heal-on-every-run-while-healthy.
    assert healthcheck.decide(healthy=False, failures=0, threshold=0) == ("heal", 0)


def test_failure_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(healthcheck, "_STATE_PATH", tmp_path / "health_state.json")
    assert healthcheck.load_failures() == 0  # no state = fresh streak
    healthcheck.save_failures(2)
    assert healthcheck.load_failures() == 2


def test_corrupt_state_reads_as_fresh(tmp_path, monkeypatch):
    state = tmp_path / "health_state.json"
    state.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(healthcheck, "_STATE_PATH", state)
    assert healthcheck.load_failures() == 0
