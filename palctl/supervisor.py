"""What palctl should do about a server that isn't answering — as one pure
decision, in one place.

The logic here used to be spread across `_maybe_autorecover` as an imperative
sequence of guard clauses reading eight flags on the Daemon object, where the
correctness lived in the *order* they were consulted. Two bugs came straight out
of that arrangement: a boot-time start racing the external-stop detector, and a
server that had never started being reported as one somebody stopped. Each fix
added another flag and another ordering rule to the pile.

So the decision is a function of an `Observation` and nothing else. It returns
an `Action` (what to do) and a `why` (in the admin's words, not the code's) —
which is also what the daemon writes to its decision log, so "why isn't palctl
doing anything" has a recorded answer instead of an inference.

Everything in this module is pure and synchronous. The daemon supplies the
observation, applies the action, and owns the counters; nothing here reads a
clock, a socket, or a process.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Consecutive STOPPED readings before palctl concludes an admin meant it. A
# restart cycle — palctl's own, or the service wrapper's onfailure — passes
# through STOPPED for a moment, and sampling one of those must not be read as
# "they want it off".
EXTERNAL_STOP_CONFIRM_POLLS = 3

# How long after the machine booted a daemon start still counts as "this is the
# boot". Generous: the SCM starts services early, but a cold box with a slow
# disk and a dozen Automatic services can take minutes to get here.
BOOT_INTENT_WINDOW = 900.0

# Service states that mean "this server is up, or on its way" — the readings
# that make a *later* STOPPED attributable to somebody stopping it. UNKNOWN is
# deliberately not one: a box with no such service must never arm adoption.
UP_STATES = frozenset({"RUNNING", "START_PENDING"})


class Action(StrEnum):
    """What the daemon should do about this poll. Every branch of the old
    guard sequence is one of these, named."""

    STAND_DOWN = "stand_down"           # meant to be stopped; nothing to do
    AWAIT_STARTUP = "await_startup"     # startup hasn't decided yet; hold
    CONFIRM_EXTERNAL_STOP = "confirm_external_stop"   # looks stopped; need more polls
    ADOPT_EXTERNAL_STOP = "adopt_external_stop"       # somebody stopped it; record that
    REPORT_NEVER_STARTED = "report_never_started"     # stopped, but it never came up
    IGNORE = "ignore"                   # recovery off, or never seen alive
    RESET = "reset"                     # palctl's own operation; clear counters
    COUNT_DOWN_POLL = "count_down_poll"  # a genuine outage, not confirmed yet
    THROTTLED = "throttled"             # confirmed, but at the hourly cap
    RECOVER = "recover"                 # restart it


@dataclass(frozen=True)
class Observation:
    """Everything the decision is allowed to depend on. Assembled by the daemon
    once per failed poll."""

    service_state: str          # RUNNING / STOPPED / START_PENDING / ... / UNKNOWN
    desired_running: bool       # the recorded intent (persisted across restarts)
    ever_alive: bool            # palctl has seen this server answer, ever
    seen_service_up: bool       # ...and this daemon has seen the service up
    busy: bool                  # palctl holds the operation lock
    restarting: bool            # the memory watchdog is mid-restart
    startup_pending: bool       # _restore_boot_intent hasn't finished
    recovery_enabled: bool      # watchdog.auto_restart_on_crash
    down_polls: int             # consecutive confirming polls so far
    external_stop_polls: int    # consecutive STOPPED readings so far
    recent_restarts: int        # auto-recoveries in the last hour
    confirm_polls: int          # watchdog.crash_confirm_polls
    restart_cap: int            # watchdog.crash_restart_max_per_hour


@dataclass(frozen=True)
class Decision:
    action: Action
    why: str


def needs_service_state(*, desired_running: bool, startup_pending: bool) -> bool:
    """Whether the caller has to ask the service manager at all.

    Exactly the two branches `decide` answers before it looks at
    `service_state`. It exists so the daemon can skip a subprocess per poll for
    a server that is meant to be down — without the daemon re-implementing the
    order those branches come in. A test pins the two together."""
    return desired_running and not startup_pending


def looks_externally_stopped(
    *, service_state: str, busy: bool, restarting: bool
) -> bool:
    """Did somebody stop the server *outside* palctl?

    palctl decides whether to auto-recover from one signal — "the REST API
    stopped answering" — and that signal cannot tell a crash from an admin
    stopping the service in services.msc, `sc stop`, or Task Manager. It only
    knew a stop was deliberate when it did the stopping itself, so every other
    stop read as a crash and got undone. Stop the service by hand, watch palctl
    put it straight back, repeat: the server behaves as though it cannot be
    turned off.

    The service manager already knows the difference and palctl already reads
    it. A process that crashed leaves the service RUNNING (or briefly
    START_PENDING while the wrapper's onfailure restarts it) with nothing
    answering behind it. A deliberate stop is the SCM reporting **STOPPED** —
    the state nothing but a stop request produces.

    `busy`/`restarting` exclude palctl's own operations, which pass through
    STOPPED constantly on their way to a restart.
    """
    return service_state == "STOPPED" and not busy and not restarting


def autorecover_phase(
    *,
    enabled: bool,
    ever_alive: bool,
    busy: bool,
    restarting: bool,
    desired_running: bool,
) -> str:
    """
    First half of the crash-recovery decision — the guards. Pure, so the whole
    'never fight an intentional stop' rule is testable without a live daemon.

    Returns:
      'ignore' — feature off, or the server never came up; do nothing.
      'reset'  — palctl took the server down on purpose (stop/restart/update/
                 restore/watchdog); clear the down-streak, do nothing.
      'count'  — a genuine unexpected outage; count this poll toward recovery.
    """
    if not enabled or not ever_alive:
        return "ignore"
    if busy or restarting or not desired_running:
        return "reset"
    return "count"


def should_recover_now(
    *, down_polls: int, confirm_polls: int, recent_restarts: int, cap: int
) -> bool:
    """Second half: only recover after N confirming polls, and not if we've
    already restarted `cap` times this hour (a real crash-loop needs a human)."""
    if down_polls < max(1, confirm_polls):
        return False
    return recent_restarts < cap


def is_boot_start(now: float, boot_time: float, window: float = BOOT_INTENT_WINDOW) -> bool:
    """Whether this daemon start is the machine coming up, rather than the
    daemon being restarted on an already-running box.

    The distinction is what keeps the boot-time intent restore narrow. Restoring
    the recorded intent at *boot* only moves a start that the SCM's Automatic
    startmode used to do anyway — same behaviour, decided by palctl instead of
    Windows. Doing it on any daemon start would be new behaviour, and the wrong
    kind: the installer bounces the daemon on upgrade and the health task
    restarts it during an outage, so a server that was down would be started by
    the side door — bypassing `auto_restart_on_crash`, which is opt-in precisely
    because restarting someone's server unasked is not palctl's call.
    """
    return 0.0 <= now - boot_time <= window


def decide(obs: Observation) -> Decision:
    """The whole 'the server isn't answering' decision, in reading order.

    Order still matters — an intent to be stopped outranks everything, and
    startup outranks the external-stop detector — but it is now stated once,
    here, instead of being implied by where a guard clause happened to sit.
    """
    if not obs.desired_running:
        return Decision(
            Action.STAND_DOWN,
            "the server is meant to be stopped, so an unreachable server is "
            "expected — palctl will not start it",
        )

    if obs.startup_pending:
        # With the game service registered Manual, STOPPED is exactly how every
        # boot begins. Counting those polls would let the daemon adopt its own
        # not-started-yet server as somebody's deliberate stop.
        return Decision(
            Action.AWAIT_STARTUP,
            "still working out what the server should be doing after startup",
        )

    stopped = looks_externally_stopped(
        service_state=obs.service_state, busy=obs.busy, restarting=obs.restarting
    )
    if stopped and not obs.seen_service_up:
        return Decision(
            Action.REPORT_NEVER_STARTED,
            "the service has been stopped for as long as palctl has been "
            "running — it never started, so this is not somebody stopping it",
        )
    if stopped:
        remaining = EXTERNAL_STOP_CONFIRM_POLLS - obs.external_stop_polls - 1
        if remaining > 0:
            return Decision(
                Action.CONFIRM_EXTERNAL_STOP,
                f"the service reads as stopped and palctl didn't stop it — "
                f"confirming over {remaining} more poll(s) before deciding it "
                "was deliberate",
            )
        return Decision(
            Action.ADOPT_EXTERNAL_STOP,
            "the service was stopped outside palctl, confirmed over several "
            "polls — taking it as deliberate and leaving it down",
        )

    phase = autorecover_phase(
        enabled=obs.recovery_enabled,
        ever_alive=obs.ever_alive,
        busy=obs.busy,
        restarting=obs.restarting,
        desired_running=obs.desired_running,
    )
    if phase == "ignore":
        return Decision(
            Action.IGNORE,
            "auto-restart on crash is off"
            if not obs.recovery_enabled
            else "palctl has never seen this server answer, so it won't "
            "restart-loop a server that may not be installed",
        )
    if phase == "reset":
        return Decision(
            Action.RESET,
            "palctl is running an operation on the server, so this outage is "
            "its own doing",
        )

    if should_recover_now(
        down_polls=obs.down_polls + 1,
        confirm_polls=obs.confirm_polls,
        recent_restarts=obs.recent_restarts,
        cap=obs.restart_cap,
    ):
        return Decision(
            Action.RECOVER,
            "the server stopped answering, palctl didn't stop it, and the "
            "service manager doesn't report it stopped — restarting it",
        )
    if obs.down_polls + 1 >= max(1, obs.confirm_polls):
        return Decision(
            Action.THROTTLED,
            f"the server is down but palctl has already auto-restarted "
            f"{obs.recent_restarts} time(s) this hour (limit "
            f"{obs.restart_cap}) — leaving it for a human",
        )
    remaining = max(1, obs.confirm_polls) - obs.down_polls - 1
    return Decision(
        Action.COUNT_DOWN_POLL,
        f"the server missed a poll — confirming over {remaining} more before "
        "treating it as an outage",
    )
