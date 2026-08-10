"""The crash auto-recovery decision and the daemon API token gate are the two
bits of new daemon logic where a mistake is silent and dangerous (restart a
server the user stopped; let any local process drive the API). They're pinned as
pure functions here. CI installs aiohttp and discord.py so these tests really
run there; the importorskip guards are for minimal local environments, where a
clean skip beats erroring at collection (palctl.daemon imports both — aiohttp
for its API server, discord via palctl.bot at module level)."""

import asyncio
import logging
import time
import types

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("discord")

import palctl.daemon as daemon_mod  # noqa: E402
from palctl.daemon import (  # noqa: E402  (after importorskip guard)
    _within_window,
    lan_exposure_warning,
    make_auth_middleware,
)
from palctl.decisions import DecisionLog  # noqa: E402
from palctl.localauth import TOKEN_HEADER  # noqa: E402

# ---------------- LAN-exposure warning ----------------


def test_lan_exposure_warning_silent_on_loopback():
    for host in ("127.0.0.1", "localhost", "::1", ""):
        assert lan_exposure_warning(host) is None, host


def test_lan_exposure_warning_fires_off_loopback():
    for host in ("0.0.0.0", "192.168.1.10"):
        msg = lan_exposure_warning(host)
        assert msg is not None
        assert host in msg
        assert "port-forward" in msg.lower()  # the one thing they must not do


def test_within_window_keeps_recent_drops_old():
    now = 10_000.0
    times = [now - 4000, now - 3599, now - 100, now - 1]
    kept = _within_window(times, now, window=3600)
    assert kept == [now - 3599, now - 100, now - 1]  # the 4000s-old one is dropped


def test_within_window_empty():
    assert _within_window([], 123.0) == []


def test_within_window_all_recent():
    now = 500.0
    times = [now - 10, now - 20, now - 30]
    assert _within_window(times, now, window=3600) == times


# ---------------- API token gate ----------------


async def _ok_handler(_req):
    return "OK"


def test_auth_middleware_allows_correct_token():
    mw = make_auth_middleware("s3cret")
    req = types.SimpleNamespace(headers={TOKEN_HEADER: "s3cret"})
    assert asyncio.run(mw(req, _ok_handler)) == "OK"


def test_auth_middleware_rejects_missing_and_wrong_token():
    mw = make_auth_middleware("s3cret")
    for headers in ({}, {TOKEN_HEADER: "wrong"}):
        # method/path/remote are what the middleware logs on a rejection.
        req = types.SimpleNamespace(
            headers=headers, method="GET", path="/state", remote="127.0.0.1"
        )
        res = asyncio.run(mw(req, _ok_handler))
        assert res.status == 401


def test_auth_middleware_exempts_only_the_named_paths():
    # "/" serves the dashboard page (no data); everything else keeps the gate.
    mw = make_auth_middleware("s3cret", exempt=frozenset({"/"}))
    page = types.SimpleNamespace(headers={}, path="/", method="GET", remote="::1")
    assert asyncio.run(mw(page, _ok_handler)) == "OK"
    data = types.SimpleNamespace(headers={}, path="/state", method="GET", remote="::1")
    assert asyncio.run(mw(data, _ok_handler)).status == 401


# ---------------- machine-account detection ----------------


def test_service_account_warning_flags_machine_accounts():
    from palctl.daemon import service_account_warning

    for name in ("SYSTEM", "system", "GAMEBOX$"):
        msg = service_account_warning(name, r"C:\odd\appdata\palctl")
        assert msg and "install-service --as-user" in msg


def test_service_account_warning_quiet_for_real_users():
    from palctl.daemon import service_account_warning

    assert service_account_warning("steve", "/home/steve/.config/palctl") is None


def test_desired_running_persists_across_restarts(tmp_path, monkeypatch):
    # An admin's Stop must survive a daemon restart — otherwise the 06:00
    # schedule resurrects a server that was taken down for maintenance.
    import palctl.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")

    assert daemon_mod._load_desired_running() is True  # first run: no state file
    daemon_mod._save_desired_running(False)  # the admin hits Stop
    assert daemon_mod._load_desired_running() is False  # the "restart" remembers
    daemon_mod._save_desired_running(True)
    assert daemon_mod._load_desired_running() is True


def test_desired_running_tolerates_garbage_state(tmp_path, monkeypatch):
    import palctl.daemon as daemon_mod

    state = tmp_path / "daemon_state.json"
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", state)
    state.write_text("{not json")
    assert daemon_mod._load_desired_running() is True  # fail open to normal behavior


# ---------------- switching startup modes cleans up the old one ----------------



# ---------------- reload-config vs. the Discord bot ----------------

# The GUI's one save button hits /action/reload-config. The trap this pins:
# enabling the bot (or fixing a rejected token) after the daemon is up used to
# do nothing until a full daemon restart, because run_bot reads enabled+token
# exactly once. _reload_bot must relaunch a finished run_bot, and must NOT
# stack a second one on top of a live/retrying one.


def _bare_daemon(bot, task):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)  # skip the heavy __init__
    d.bot = bot
    d._bot_task = task
    d._started = 0
    d._start_bot = lambda: setattr(d, "_started", d._started + 1)
    return d


class _FakeBot:
    def __init__(self):
        self.reconfigured_with = None

    def reconfigure(self, cfg, api):
        self.reconfigured_with = (cfg, api)


class _FakeTask:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def test_reload_relaunches_finished_bot():
    # Bot was never started (disabled / no token at boot), user saves settings.
    d = _bare_daemon(bot=None, task=_FakeTask(done=True))
    d._reload_bot()
    assert d._started == 1


def test_reload_clears_dead_client_before_relaunch():
    # LoginFailure leaves run_bot returned but self.bot pointing at the dead
    # client; a relaunch must not reconfigure that corpse instead of starting.
    dead = _FakeBot()
    d = _bare_daemon(bot=dead, task=_FakeTask(done=True))
    d._reload_bot()
    assert d._started == 1
    assert d.bot is None  # run_bot's on_created will set the real one
    assert dead.reconfigured_with is None


def test_reload_reconfigures_live_bot_without_relaunch():
    live = _FakeBot()
    d = _bare_daemon(bot=live, task=_FakeTask(done=False))
    d.cfg, d.api = object(), object()
    d._reload_bot()
    assert d._started == 0
    assert live.reconfigured_with == (d.cfg, d.api)


def test_reload_leaves_retrying_run_bot_alone():
    # run_bot in its connect-retry backoff: task not done, and self.bot points
    # at the latest attempt. Reconfigure it, don't start a second run_bot.
    attempt = _FakeBot()
    d = _bare_daemon(bot=attempt, task=_FakeTask(done=False))
    d.cfg, d.api = object(), object()
    d._reload_bot()
    assert d._started == 0
    assert attempt.reconfigured_with == (d.cfg, d.api)


def test_reload_before_run_started_is_harmless():
    d = _bare_daemon(bot=None, task=None)
    d._reload_bot()  # no crash, nothing started
    assert d._started == 0


# ---------------- sd_notify (systemd liveness) ----------------

import socket  # noqa: E402
import sys  # noqa: E402


def test_sd_notify_is_silent_without_a_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    daemon_mod.sd_notify("READY=1")  # must not raise, nothing to send to


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="AF_UNIX datagram")
def test_sd_notify_sends_to_the_notify_socket(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        daemon_mod.sd_notify("WATCHDOG=1")
        assert srv.recv(64) == b"WATCHDOG=1"
    finally:
        srv.close()


# ---------------- log tail endpoint helper ----------------


def test_tail_log_file_returns_last_n_lines(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "palctl.log").write_text("".join(f"line {i}\n" for i in range(100)), "utf-8")
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    out = daemon_mod._tail_log_file(5)
    assert out.splitlines() == ["line 95", "line 96", "line 97", "line 98", "line 99"]


def test_tail_log_file_missing_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "config_dir", lambda: tmp_path)
    assert "no daemon log" in daemon_mod._tail_log_file(10)


# ---------------- low-disk helper ----------------


def test_lowest_free_gb_takes_the_tighter_volume(monkeypatch):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d.cfg = daemon_mod.Config()
    d.cfg.server_root = "/srv"
    d.cfg.backup_root = "/backups"
    import shutil

    def fake_usage(path):
        free = (50 if path == "/srv" else 3) * (1024**3)
        return types.SimpleNamespace(total=0, used=0, free=free)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    assert d._lowest_free_gb() == 3.0  # the backup volume is the tighter one


# ---------------- startup side effects stay off the event loop ----------------
#
# These two chores shell out (netsh; rclone version) and run *after* the HTTP
# site is bound. Doing them on the loop meant a daemon whose port accepted
# connections and then answered nothing at all — the worst shape of hang,
# because every "is the port open?" check says yes while the app is dead. netsh
# against a sick Windows Firewall service is exactly the slow case.


def test_startup_side_effects_do_not_block_the_event_loop():
    import time as _time

    calls = []

    class _Stub:
        def _sync_dashboard_firewall(self, host):
            calls.append(("firewall", host))
            _time.sleep(0.5)  # a slow netsh

        def _warn_if_cloud_mirror_broken(self):
            calls.append(("mirror",))
            _time.sleep(0.5)  # a slow rclone

        async def _check_settings_drift(self):
            # Reads and parses the ini; same rule — it must not sit on the loop.
            calls.append(("drift",))
            await asyncio.to_thread(_time.sleep, 0.5)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        await daemon_mod.Daemon._startup_side_effects(_Stub(), "0.0.0.0")
        t.cancel()
        return ticks

    ticks = asyncio.run(main())
    # Both chores ran...
    assert calls == [("firewall", "0.0.0.0"), ("drift",), ("mirror",)]
    # ...and the loop kept running throughout, instead of freezing for ~1s.
    assert ticks > 10, f"event loop was blocked (only {ticks} ticks in ~1s)"


# ---------------- /healthz is about THIS DAEMON, not the game server ----------
#
# The only consumer that acts on /healthz is the scheduled health task, and its
# remedy is restarting the daemon. Stamping the liveness clock only on a
# *successful* poll made an unreachable game server read as a wedged daemon, so
# the task restarted a perfectly healthy daemon about a quarter of an hour into
# every outage — killing the auto-recovery that was working the problem, and
# resetting the crash-restart budget that exists to stop restart loops.


def test_poll_loop_is_live_while_starting_up():
    ok, age = daemon_mod.poll_loop_is_live(last_poll_at=0.0, now=1000.0, poll_seconds=10)
    assert ok is True and age is None


def test_poll_loop_is_live_on_a_recent_cycle():
    ok, age = daemon_mod.poll_loop_is_live(
        last_poll_at=1000.0, now=1005.0, poll_seconds=10
    )
    assert ok is True and age == 5.0


def test_poll_loop_is_not_live_once_cycles_stop():
    """The case the probe exists for: a wedged loop or a dead poller."""
    ok, age = daemon_mod.poll_loop_is_live(
        last_poll_at=1000.0, now=1100.0, poll_seconds=10
    )
    assert ok is False and age == 100.0


def test_poll_loop_liveness_floor_protects_a_fast_poll_interval():
    """poll_seconds * 6 would be 6s at a 1s interval — far too tight to survive
    an ordinary hiccup. The 30s floor is what stops that being a restart loop."""
    ok, _ = daemon_mod.poll_loop_is_live(last_poll_at=1000.0, now=1020.0, poll_seconds=1)
    assert ok is True


def test_poll_loop_stamps_liveness_even_when_the_poll_fails():
    """The regression: a poll that couldn't reach the game server is still a
    completed cycle, so it must refresh the liveness clock."""

    class _Stub:
        cfg = types.SimpleNamespace(poll_seconds=1)
        _last_poll_at = 0.0
        emitted: list = []

        class bus:
            @staticmethod
            async def emit(e):
                _Stub.emitted.append(e)

        async def _poll(self):
            raise RuntimeError("Palworld REST API unreachable")

    stub = _Stub()

    async def main():
        task = asyncio.create_task(daemon_mod.Daemon._poll_loop(stub))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert stub._last_poll_at > 0.0, "a failed poll must still count as a cycle"
    assert _Stub.emitted, "the failure should still be reported as an event"


# ---------------- ever_alive survives a daemon restart ----------------


def test_ever_alive_is_persisted_so_an_outage_can_span_a_restart(tmp_path, monkeypatch):
    """Auto-recovery refuses to touch a server it has never seen up. Losing that
    flag on restart meant a daemon that bounced *during* an outage would never
    recover the server — it just stayed down."""
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")

    assert daemon_mod._load_ever_alive() is False
    daemon_mod._save_ever_alive(True)
    assert daemon_mod._load_ever_alive() is True


def test_state_file_keeps_both_keys(tmp_path, monkeypatch):
    """The two flags share one file; writing either must not drop the other."""
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")

    daemon_mod._save_desired_running(False)
    daemon_mod._save_ever_alive(True)
    assert daemon_mod._load_desired_running() is False
    assert daemon_mod._load_ever_alive() is True

    daemon_mod._save_desired_running(True)
    assert daemon_mod._load_ever_alive() is True  # not clobbered


def test_unreadable_state_falls_back_to_safe_defaults(tmp_path, monkeypatch):
    path = tmp_path / "daemon_state.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", path)
    # desired_running defaults True (normal behaviour); ever_alive defaults
    # False (never auto-recover a server we have no evidence ever worked).
    assert daemon_mod._load_desired_running() is True
    assert daemon_mod._load_ever_alive() is False


# ---------------- "down, and I'm not allowed to fix it" ----------------
#
# auto_restart_on_crash is opt-in on purpose — restarting someone's server
# unasked isn't a default to take lightly. The silence around it is the problem:
# watched from outside, a real hang produces one "🔴 Server is down." and then
# nothing, ever. That is indistinguishable from palctl being broken, and it is
# the likeliest reason someone concludes that it is.


def _daemon_for_recovery_notice(*, enabled: bool, alive, ever_alive: bool):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d.cfg = daemon_mod.Config()
    d.cfg.watchdog.auto_restart_on_crash = enabled
    d._alive = alive
    d.__dict__["_Daemon__ever_alive"] = ever_alive
    d._recovery_off_warned = False
    d.emitted = []

    class _Bus:
        @staticmethod
        async def emit(e):
            d.emitted.append(e)

    d.bus = _Bus()
    return d


def _notices(d):
    return [e for e in d.emitted if e.data.get("action") == "recovery_disabled"]


def test_a_confirmed_outage_says_recovery_is_off():
    d = _daemon_for_recovery_notice(enabled=False, alive=False, ever_alive=True)
    asyncio.run(d._warn_recovery_is_off(d.cfg.watchdog))
    assert len(_notices(d)) == 1
    assert "auto_restart_on_crash" in _notices(d)[0].message


def test_the_notice_is_once_per_outage_not_once_per_poll():
    d = _daemon_for_recovery_notice(enabled=False, alive=False, ever_alive=True)
    for _ in range(5):
        asyncio.run(d._warn_recovery_is_off(d.cfg.watchdog))
    assert len(_notices(d)) == 1


def test_no_notice_while_auto_recovery_is_doing_its_job():
    d = _daemon_for_recovery_notice(enabled=True, alive=False, ever_alive=True)
    asyncio.run(d._warn_recovery_is_off(d.cfg.watchdog))
    assert _notices(d) == []


def test_no_notice_before_the_outage_is_confirmed():
    """Same threshold as the down announcement — one slow poll isn't an outage."""
    d = _daemon_for_recovery_notice(enabled=False, alive=True, ever_alive=True)
    asyncio.run(d._warn_recovery_is_off(d.cfg.watchdog))
    assert _notices(d) == []


def test_no_notice_for_a_server_palctl_has_never_seen_working():
    """A box with no server installed shouldn't be nagged about recovering it."""
    d = _daemon_for_recovery_notice(enabled=False, alive=False, ever_alive=False)
    asyncio.run(d._warn_recovery_is_off(d.cfg.watchdog))
    assert _notices(d) == []


def test_an_inconclusive_account_check_does_not_burn_the_one_shot():
    """The regression: the flag was latched before the check ran, so one poll
    where the process wasn't readable suppressed the warning for the whole
    daemon run — on exactly the boxes where the split makes it unreadable."""
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d._account_warned = False
    d.log = types.SimpleNamespace(warning=lambda *a, **k: None)
    d.emitted = []

    class _Bus:
        @staticmethod
        async def emit(e):
            d.emitted.append(e)

    d.bus = _Bus()

    results = iter([(False, None), (True, "the server runs as SYSTEM")])
    daemon_mod.procs.server_account_check = lambda user: next(results)
    try:
        asyncio.run(d._maybe_warn_account_mismatch())
        assert d._account_warned is False, "an inconclusive check must not latch"
        assert d.emitted == []

        asyncio.run(d._maybe_warn_account_mismatch())
        assert d._account_warned is True
        assert len(d.emitted) == 1, "the real answer still gets reported once"
    finally:
        import importlib

        importlib.reload(daemon_mod.procs)


def test_the_raids_hint_appears_only_when_raids_are_on(tmp_path):
    """It's advice about someone's gameplay, attached to the leak forecast —
    the one moment they're definitely thinking about memory. It must not fire
    when raids are already off, or when palctl couldn't read the ini."""
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d.cfg = daemon_mod.Config()
    d.cfg.server_root = str(tmp_path)
    ini = d.cfg.live_ini
    ini.parent.mkdir(parents=True, exist_ok=True)

    ini.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(bEnableInvaderEnemy=True)\n",
        encoding="utf-8",
    )
    assert "bEnableInvaderEnemy" in asyncio.run(d._raids_hint())

    ini.write_text(
        "[/Script/Pal.PalGameWorldSettings]\n"
        "OptionSettings=(bEnableInvaderEnemy=False)\n",
        encoding="utf-8",
    )
    assert asyncio.run(d._raids_hint()) == ""

    ini.unlink()
    assert asyncio.run(d._raids_hint()) == "", "an unreadable ini must say nothing"


# ---------------- boot-time intent restore ----------------


def _daemon_for_boot_intent(state: str, *, desired=True, after_start="RUNNING"):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d.__dict__["_Daemon__desired_running"] = desired
    d._boot_intent_pending = True
    d.decisions = DecisionLog()
    d.log = logging.getLogger("test-boot-intent")
    d.cfg = types.SimpleNamespace(service_name="PalServer")
    d.emitted = []
    d.started = []
    states = [state, after_start]

    async def _state(ttl=2.0):
        return states[0] if len(states) == 1 else states.pop(0)

    d._service_state_cached = _state

    async def _start():
        d.started.append(True)
        return "ok"

    d.scheduler = types.SimpleNamespace(start_server=_start)

    class _Bus:
        @staticmethod
        async def emit(e):
            d.emitted.append(e)

    d.bus = _Bus()
    return d


def _at_boot(monkeypatch, tmp_path, *, booted_secs_ago=5.0):
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")
    monkeypatch.setattr(
        daemon_mod.procs, "boot_time", lambda: time.time() - booted_secs_ago
    )


def test_boot_restores_a_server_that_should_be_running(monkeypatch, tmp_path):
    """The other half of registering PalServer Manual: if the SCM no longer
    starts it at boot, palctl has to."""
    _at_boot(monkeypatch, tmp_path)
    d = _daemon_for_boot_intent("STOPPED")
    asyncio.run(d._restore_boot_intent())
    assert d.started == [True]
    assert d._boot_intent_pending is False


def test_boot_leaves_a_deliberately_stopped_server_alone(monkeypatch, tmp_path):
    """The whole point of the change — a Stop that survives a restart."""
    _at_boot(monkeypatch, tmp_path)
    d = _daemon_for_boot_intent("STOPPED", desired=False)
    asyncio.run(d._restore_boot_intent())
    assert d.started == []
    assert d._boot_intent_pending is False


def test_a_daemon_restart_on_a_running_box_starts_nothing(monkeypatch, tmp_path):
    """An upgrade or the health task restarting the daemon must not start a
    server that is down — that is auto-recovery's opt-in call, not this."""
    _at_boot(monkeypatch, tmp_path, booted_secs_ago=6 * 3600)
    d = _daemon_for_boot_intent("STOPPED")
    asyncio.run(d._restore_boot_intent())
    assert d.started == []
    assert d._boot_intent_pending is False


def test_boot_does_not_touch_an_already_running_service(monkeypatch, tmp_path):
    _at_boot(monkeypatch, tmp_path)
    d = _daemon_for_boot_intent("RUNNING")
    asyncio.run(d._restore_boot_intent())
    assert d.started == []


def test_a_failed_boot_start_is_reported_as_a_failure_to_start(monkeypatch, tmp_path):
    """Not as somebody stopping it: the intent stays 'should be running', and
    the message says which of the two this is."""
    _at_boot(monkeypatch, tmp_path)
    d = _daemon_for_boot_intent("STOPPED", after_start="STOPPED")
    asyncio.run(d._restore_boot_intent())
    assert d.started == [True]
    assert [e for e in d.emitted if e.data.get("action") == "boot_start_failed"]
    assert d._desired_running is True


def test_boot_intent_always_releases_the_startup_hold(monkeypatch, tmp_path):
    """A failure here must not leave adoption and recovery blocked forever."""
    _at_boot(monkeypatch, tmp_path)
    d = _daemon_for_boot_intent("STOPPED")

    async def _boom(ttl=2.0):
        raise RuntimeError("sc.exe fell over")

    d._service_state_cached = _boom
    asyncio.run(d._restore_boot_intent())
    assert d._boot_intent_pending is False
