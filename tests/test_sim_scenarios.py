"""End-to-end scenarios against a simulated machine (tests/sim).

Every scenario here is a failure that shipped past a green test suite, or a
guarantee that only means anything end to end. They are deliberately written as
situations rather than units: "the server wedges", "somebody stopped it in
services.msc", "the box rebooted" — because the bugs were never in the pieces,
they were in what palctl concluded from a real environment.

Slow by nature (each boots the real daemon and a real fake server). Linux-only:
the fake service manager is a `systemctl` on PATH.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("discord")

from tests.sim.harness import Sim, running_server  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        sys.platform.startswith("win"), reason="the fake service manager is a POSIX shim"
    ),
    pytest.mark.sim,
]


# ---------------- the server misbehaves ----------------


def test_a_wedged_server_is_noticed_and_recovered(sim):
    """The original report: a server that accepts connections and never answers.
    It is not a refused connection, and palctl has to come back from it."""
    running_server(sim)

    sim.set_server_mode("hang")
    sim.wait_for(
        lambda: sim.state()["alive"] is False, timeout=60, what="the server to read as down"
    )
    sim.wait_for(
        lambda: any("recover" in e.lower() for e in sim.events()),
        timeout=90,
        what="auto-recovery to fire",
    )
    sim.wait_for(lambda: sim.state()["alive"] is True, timeout=120, what="the server to come back")


def test_the_daemon_stays_answerable_while_the_server_hangs(sim):
    """"The whole system locks up" — a hung server must cost palctl nothing.
    A blocking call on the poll path would take the control API down with it,
    which is what makes the app itself look dead."""
    import time

    import httpx

    from tests.sim.harness import BASE

    running_server(sim)
    sim.set_server_mode("hang")

    # Every poll is now stuck on a server that will never answer. The control
    # API must not be: these are the calls the GUI, the CLI and any external
    # health check make, and if they queue behind the poll, palctl is what
    # looks hung.
    for _ in range(8):
        began = time.monotonic()
        assert sim.state() is not None
        assert httpx.get(f"{BASE}/healthz", timeout=10).status_code == 200
        took = time.monotonic() - began
        assert took < 5.0, f"the control API took {took:.1f}s while the server hung"
        time.sleep(1.0)


def test_health_stays_ok_while_the_server_is_down(sim):
    """/healthz is the daemon's liveness, not the game server's. Conflating them
    had an external health check restarting a perfectly healthy daemon in the
    middle of its own recovery."""
    import httpx

    from tests.sim.harness import BASE

    running_server(sim)
    sim.systemctl("stop")
    sim.wait_for(
        lambda: sim.state()["alive"] is False, timeout=60, what="the server to read as down"
    )

    r = httpx.get(f"{BASE}/healthz", timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------- somebody else drives the service ----------------


def test_a_stop_from_outside_palctl_is_taken_as_deliberate(sim):
    """The 'unstoppable server': stopping the service any way but palctl's own
    Stop read as a crash and was undone within seconds."""
    running_server(sim)

    sim.systemctl("stop")
    sim.wait_for(
        lambda: any("stopped outside palctl" in e for e in sim.events()),
        timeout=60,
        what="palctl to notice the external stop",
    )
    assert sim.read_daemon_state().get("desired_running") is False
    sim.stays(
        lambda: sim.service_state() == "inactive",
        seconds=12,
        what="the server staying stopped",
    )
    # ...and palctl's own Start still undoes it.
    assert sim.post("/action/start").status_code == 200
    sim.wait_for(lambda: sim.service_state() == "active", timeout=60, what="Start to work")


def test_an_adopted_stop_survives_a_daemon_restart(sim):
    """Recording the intent is what makes it stick: without persistence the
    schedule and auto-recovery resurrect the server after the next restart."""
    running_server(sim)
    sim.systemctl("stop")
    sim.wait_for(
        lambda: sim.read_daemon_state().get("desired_running") is False,
        timeout=60,
        what="the stop to be recorded",
    )

    sim.stop_daemon()
    sim.start_daemon()
    sim.stays(
        lambda: sim.service_state() == "inactive",
        seconds=10,
        what="the server staying stopped across a daemon restart",
    )


# ---------------- the machine reboots ----------------


def test_a_reboot_brings_back_a_server_that_should_be_running(tmp_path):
    """The game service is registered Manual when palctl is a boot service, so
    palctl is the only thing that will start it."""
    s = Sim(tmp_path)
    s.write_config()
    s.write_daemon_state(desired_running=True, ever_alive=True)
    try:
        s.start_daemon(pretend_boot=True)
        s.wait_for(
            lambda: s.service_state() == "active", timeout=60, what="the server to be started"
        )
        s.wait_for(lambda: s.state()["alive"] is True, timeout=60, what="the server to answer")
    finally:
        s.stop_daemon()
        s.systemctl("stop")


def test_a_reboot_leaves_a_deliberately_stopped_server_stopped(tmp_path):
    """The reboot half of the unstoppable-server complaint. Auto-recovery is
    ENABLED here on purpose: a server that was stopped on purpose must not be
    'recovered' either."""
    s = Sim(tmp_path)
    s.write_config()
    s.write_daemon_state(desired_running=False, ever_alive=True)
    try:
        s.start_daemon(pretend_boot=True)
        s.stays(
            lambda: s.service_state() == "inactive",
            seconds=12,
            what="the server staying stopped after a reboot",
        )
        assert s.service_calls() == [], "palctl must not have touched the service"
    finally:
        s.stop_daemon()


def test_a_daemon_restart_that_is_not_a_boot_starts_nothing(tmp_path):
    """The installer bounces the daemon on upgrade and the health task restarts
    it mid-outage. Restoring intent there would start a downed server behind the
    admin's back, bypassing the opt-in auto_restart_on_crash."""
    s = Sim(tmp_path)
    s.write_config(watchdog={"enabled": True, "auto_restart_on_crash": False})
    s.write_daemon_state(desired_running=True, ever_alive=True)
    try:
        s.start_daemon()  # a normal start: boot was hours ago
        s.stays(
            lambda: s.service_state() == "inactive",
            seconds=10,
            what="the server staying down on a non-boot daemon start",
        )
        assert s.service_calls() == []
    finally:
        s.stop_daemon()


def test_a_server_that_fails_to_start_at_boot_is_not_blamed_on_an_admin(tmp_path):
    """A service that never starts is a failure to start, not somebody stopping
    it — and saying the wrong one sends the admin hunting a culprit that doesn't
    exist. The intent must also survive, so a later fix brings it back."""
    s = Sim(tmp_path)
    s.write_config()
    s.write_daemon_state(desired_running=True, ever_alive=True)
    s.set_service_mode("refuse_start")
    try:
        s.start_daemon(pretend_boot=True)
        # palctl says what it's doing straight away...
        s.wait_for(
            lambda: any(d["action"] == "boot_start" for d in s.decisions()),
            timeout=30,
            what="palctl to record that it is starting the server",
        )
        # ...and reports the failure once the service manager gives up. Slow on
        # purpose: procs.start_service waits out a full start timeout, which is
        # the behaviour a real slow-starting PalServer depends on.
        s.wait_for(
            lambda: any("didn't reach RUNNING" in e for e in s.events()),
            timeout=200,
            what="the failed start to be reported",
        )
        assert not any("stopped outside palctl" in e for e in s.events())
        assert s.read_daemon_state().get("desired_running") is True
    finally:
        s.stop_daemon()
