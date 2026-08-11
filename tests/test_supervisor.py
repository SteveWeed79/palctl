"""The 'what do we do about a server that isn't answering' decision.

This used to be an imperative sequence of guard clauses on the Daemon object,
and its two most recent bugs were both ordering mistakes between those clauses
— one where a boot-time start raced the external-stop detector, one where a
server that had never started was reported as one somebody stopped. Neither was
reachable without booting a daemon.

As a pure function it is a table, so the whole policy can be read (and pinned)
in one place. `palctl.supervisor` imports nothing from the daemon and touches no
clock, socket, or process, so none of this needs aiohttp or discord.
"""

from palctl.supervisor import (
    BOOT_INTENT_WINDOW,
    EXTERNAL_STOP_CONFIRM_POLLS,
    Action,
    Observation,
    autorecover_phase,
    decide,
    is_boot_start,
    looks_externally_stopped,
    needs_service_state,
    should_recover_now,
)

# A server that has crashed: it was up, palctl didn't stop it, the service still
# reads RUNNING, and recovery is on. Every case below is this with one thing
# changed, which is the point — the table shows what each input is worth.
_CRASHED = {
    "service_state": "RUNNING",
    "desired_running": True,
    "ever_alive": True,
    "seen_service_up": True,
    "busy": False,
    "restarting": False,
    "startup_pending": False,
    "recovery_enabled": True,
    "down_polls": 0,
    "external_stop_polls": 0,
    "recent_restarts": 0,
    "confirm_polls": 1,
    "restart_cap": 3,
}


def obs(**over) -> Observation:
    return Observation(**{**_CRASHED, **over})


# ---------------- the guards, in the order they apply ----------------


def test_an_intentional_stop_outranks_everything():
    """Including a service the SCM says is RUNNING: if palctl was told to keep
    it down, an unreachable server is the expected outcome, not a problem."""
    d = decide(obs(desired_running=False))
    assert d.action is Action.STAND_DOWN
    assert "meant to be stopped" in d.why


def test_startup_outranks_the_external_stop_detector():
    """With the game service registered Manual, STOPPED is how every boot
    begins — counting those polls would let palctl adopt its own
    not-started-yet server as somebody's deliberate stop."""
    d = decide(obs(service_state="STOPPED", startup_pending=True))
    assert d.action is Action.AWAIT_STARTUP


def test_a_stop_palctl_didnt_make_is_confirmed_then_adopted():
    for polls in range(EXTERNAL_STOP_CONFIRM_POLLS - 1):
        d = decide(obs(service_state="STOPPED", external_stop_polls=polls))
        assert d.action is Action.CONFIRM_EXTERNAL_STOP, polls
    d = decide(
        obs(service_state="STOPPED", external_stop_polls=EXTERNAL_STOP_CONFIRM_POLLS - 1)
    )
    assert d.action is Action.ADOPT_EXTERNAL_STOP


def test_a_service_that_never_started_is_not_somebody_stopping_it():
    """Saying the wrong one of these sends the admin hunting a culprit that
    doesn't exist — and would latch the intent off on a server that simply
    failed to start."""
    d = decide(obs(service_state="STOPPED", seen_service_up=False))
    assert d.action is Action.REPORT_NEVER_STARTED
    assert "never started" in d.why


def test_palctls_own_operations_are_never_read_as_an_admin_stopping_it():
    """A restart passes through STOPPED on its way back up."""
    for flag in ("busy", "restarting"):
        d = decide(obs(service_state="STOPPED", **{flag: True}))
        assert d.action is Action.RESET, flag


def test_recovery_off_is_reported_not_silently_ignored():
    d = decide(obs(recovery_enabled=False))
    assert d.action is Action.IGNORE
    assert "auto-restart on crash is off" in d.why


def test_a_server_never_seen_alive_is_left_alone():
    """No restart-looping a box where the server was never installed."""
    d = decide(obs(ever_alive=False))
    assert d.action is Action.IGNORE
    assert "never seen" in d.why


def test_a_crash_is_confirmed_over_polls_then_recovered():
    d = decide(obs(confirm_polls=3, down_polls=0))
    assert d.action is Action.COUNT_DOWN_POLL
    d = decide(obs(confirm_polls=3, down_polls=1))
    assert d.action is Action.COUNT_DOWN_POLL
    d = decide(obs(confirm_polls=3, down_polls=2))
    assert d.action is Action.RECOVER


def test_a_crash_loop_is_handed_to_a_human():
    d = decide(obs(recent_restarts=3, restart_cap=3))
    assert d.action is Action.THROTTLED
    assert "3 time(s) this hour" in d.why


def test_every_decision_explains_itself():
    """The `why` is not decoration — it's what /state and the decision log show
    an admin asking why palctl isn't doing anything."""
    cases = [
        obs(desired_running=False),
        obs(startup_pending=True, service_state="STOPPED"),
        obs(service_state="STOPPED"),
        obs(service_state="STOPPED", seen_service_up=False),
        obs(recovery_enabled=False),
        obs(ever_alive=False),
        obs(busy=True),
        obs(confirm_polls=5),
        obs(recent_restarts=9, restart_cap=3),
        obs(),
    ]
    for o in cases:
        d = decide(o)
        assert d.why and d.why[0].islower(), d
        assert len(d.why) > 20, d  # a sentence, not a status token


def test_needs_service_state_agrees_with_the_branches_that_ignore_it():
    """The daemon skips a subprocess per poll when the answer can't depend on
    the service state. That shortcut is only safe while these two agree."""
    for desired in (True, False):
        for pending in (True, False):
            if needs_service_state(desired_running=desired, startup_pending=pending):
                continue
            # Whatever the service says, the decision must not depend on it.
            actions = {
                decide(
                    obs(
                        desired_running=desired,
                        startup_pending=pending,
                        service_state=state,
                    )
                ).action
                for state in ("RUNNING", "STOPPED", "UNKNOWN", "START_PENDING")
            }
            assert actions <= {Action.STAND_DOWN, Action.AWAIT_STARTUP}
            assert len(actions) == 1, (desired, pending, actions)


# ---------------- the predicates the table is built from ----------------


def test_a_deliberate_stop_is_recognised():
    assert looks_externally_stopped(
        service_state="STOPPED", busy=False, restarting=False
    )


def test_a_crashed_server_is_not_mistaken_for_a_deliberate_stop():
    """The distinction the SCM already knows: a crash leaves the service
    RUNNING (or START_PENDING while the wrapper restarts it) with nothing
    answering behind it. Only a stop request produces STOPPED."""
    for state in ("RUNNING", "START_PENDING", "STOP_PENDING", "UNKNOWN"):
        assert not looks_externally_stopped(
            service_state=state, busy=False, restarting=False
        ), state


_CLEAR = {
    "enabled": True,
    "ever_alive": True,
    "busy": False,
    "restarting": False,
    "desired_running": True,
}


def test_phase_ignore_when_disabled_or_never_alive():
    assert autorecover_phase(**{**_CLEAR, "enabled": False}) == "ignore"
    assert autorecover_phase(**{**_CLEAR, "ever_alive": False}) == "ignore"


def test_phase_reset_on_intentional_downtime():
    # busy (update/restore), a watchdog restart, or a user "Stop" all mean the
    # outage was on purpose — never auto-recover through those.
    assert autorecover_phase(**{**_CLEAR, "busy": True}) == "reset"
    assert autorecover_phase(**{**_CLEAR, "restarting": True}) == "reset"
    assert autorecover_phase(**{**_CLEAR, "desired_running": False}) == "reset"


def test_phase_count_on_genuine_outage():
    assert autorecover_phase(**_CLEAR) == "count"


def test_should_recover_needs_confirmation_then_respects_cap():
    # not enough confirming polls yet
    assert should_recover_now(down_polls=1, confirm_polls=3, recent_restarts=0, cap=3) is False
    # confirmed, and under the hourly cap
    assert should_recover_now(down_polls=3, confirm_polls=3, recent_restarts=2, cap=3) is True
    # confirmed, but already at the cap this hour -> hands off, let a human look
    assert should_recover_now(down_polls=3, confirm_polls=3, recent_restarts=3, cap=3) is False


def test_is_boot_start_distinguishes_a_reboot_from_a_daemon_restart():
    boot = 1_000_000.0
    assert is_boot_start(boot + 5.0, boot) is True
    assert is_boot_start(boot + BOOT_INTENT_WINDOW, boot) is True
    # Hours into an uptime: the installer bouncing the daemon, or the health
    # task restarting it mid-outage. Not a boot, so not ours to act on.
    assert is_boot_start(boot + 6 * 3600, boot) is False
    # A clock that moved backwards must not read as "just booted".
    assert is_boot_start(boot - 60.0, boot) is False
