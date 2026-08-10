"""The decision log — palctl's record of what it chose and why.

Its whole value is being readable during an outage, so the two things worth
pinning are that repeats collapse (the poll loop revisits the same conclusion
every few seconds, and a five-hour outage must not become 4,000 lines) and that
a genuinely changed reason still shows up.
"""

from palctl.decisions import DecisionLog, summarize


def test_repeats_collapse_into_one_entry_with_a_count():
    log = DecisionLog()
    for i in range(50):
        log.record("ignore", "auto-restart on crash is off", now=1000.0 + i)

    assert len(log) == 1
    (entry,) = log.entries()
    assert entry["count"] == 50
    assert entry["first_at"] == 1000.0
    assert entry["last_at"] == 1049.0


def test_a_changed_reason_is_a_new_entry_even_for_the_same_action():
    """The confirmation countdown is one action with a changing reason, and the
    countdown is the part that explains the wait."""
    log = DecisionLog()
    log.record("confirm_external_stop", "confirming over 2 more poll(s)")
    log.record("confirm_external_stop", "confirming over 1 more poll(s)")
    assert len(log) == 2


def test_entries_are_newest_first_and_bounded():
    log = DecisionLog(maxlen=3)
    for i in range(6):
        log.record(f"action{i}", f"why {i}")
    actions = [e["action"] for e in log.entries()]
    assert actions == ["action5", "action4", "action3"]


def test_the_limit_takes_the_newest():
    log = DecisionLog()
    for i in range(10):
        log.record(f"action{i}", f"why {i}")
    assert [e["action"] for e in log.entries(2)] == ["action9", "action8"]


def test_data_is_carried_but_optional():
    log = DecisionLog()
    log.record("recover", "restarting it", data={"attempt": 2})
    assert log.entries()[0]["data"] == {"attempt": 2}
    log.record("ignore", "off")
    assert "data" not in log.entries()[0]


def test_summarize_says_how_long_a_reason_has_held():
    log = DecisionLog()
    log.record("ignore", "auto-restart on crash is off", now=0.0)
    log.record("ignore", "auto-restart on crash is off", now=600.0)
    assert summarize(log.latest()) == "auto-restart on crash is off (for 10m)"


def test_summarize_handles_an_empty_log():
    assert summarize(DecisionLog().latest()) == "no decisions recorded yet"


def test_a_brief_repeat_is_not_padded_with_a_duration():
    log = DecisionLog()
    log.record("recover", "restarting it", now=0.0)
    log.record("recover", "restarting it", now=3.0)
    assert summarize(log.latest()) == "restarting it"
