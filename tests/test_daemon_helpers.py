"""The crash auto-recovery decision and the daemon API token gate are the two
bits of new daemon logic where a mistake is silent and dangerous (restart a
server the user stopped; let any local process drive the API). They're pinned as
pure functions here. CI installs aiohttp and discord.py so these tests really
run there; the importorskip guards are for minimal local environments, where a
clean skip beats erroring at collection (palctl.daemon imports both — aiohttp
for its API server, discord via palctl.bot at module level)."""

import asyncio
import types

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("discord")

import palctl.daemon as daemon_mod  # noqa: E402
from palctl.daemon import (  # noqa: E402  (after importorskip guard)
    _within_window,
    autorecover_phase,
    lan_exposure_warning,
    make_auth_middleware,
    service_target,
    should_recover_now,
)
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


# ---------------- auto-recover state machine ----------------

_CLEAR = dict(enabled=True, ever_alive=True, busy=False, restarting=False, desired_running=True)


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


# ---------------- frozen service target ----------------


def test_service_target_frozen_resolves_daemon_exe_from_gui(tmp_path, monkeypatch):
    # The onedir frozen build ships palctl-daemon.exe and palctl-gui.exe side by
    # side. The wizard registers the daemon service from inside the GUI process,
    # so sys.executable is the GUI — but the service must still point at the
    # DAEMON exe, or the daemon never runs and every GUI action hits 10061.
    (tmp_path / "palctl-daemon.exe").write_bytes(b"MZ")
    gui = tmp_path / "palctl-gui.exe"
    gui.write_bytes(b"MZ")

    monkeypatch.setattr(daemon_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(daemon_mod.sys, "executable", str(gui))
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")

    exe, args, app_dir = service_target()
    assert exe.endswith("palctl-daemon.exe")
    assert args == ""
    assert app_dir == str(tmp_path)


def test_service_target_frozen_falls_back_when_daemon_exe_absent(tmp_path, monkeypatch):
    # Odd layout (no sibling daemon exe): register the running exe rather than a
    # path that doesn't exist.
    gui = tmp_path / "palctl-gui.exe"
    gui.write_bytes(b"MZ")

    monkeypatch.setattr(daemon_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(daemon_mod.sys, "executable", str(gui))
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")

    exe, _, _ = service_target()
    assert exe == str(gui)


def test_service_target_dev_uses_module_invocation(monkeypatch):
    monkeypatch.setattr(daemon_mod.sys, "frozen", False, raising=False)
    exe, args, _ = service_target()
    assert args == "-m palctl.daemon"


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


def test_install_service_windows_clears_login_startup_and_stray_daemon(monkeypatch):
    # Switching login startup → service: the Run key must go (or the next
    # login spawns a rival daemon), and a surviving login-startup daemon must
    # be stopped BETWEEN registration and start — otherwise the service daemon
    # can't bind the control port and the wrapper restart-loops it while the
    # old daemon keeps serving.
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    calls: list[str] = []
    registered_kwargs: dict = {}
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: calls.append("runkey"))
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")

    def fake_install(winsw, name, exe, args, app_dir, **kw):
        registered_kwargs.update(kw)
        calls.append("register")

    monkeypatch.setattr(winservice, "install_service", fake_install)
    monkeypatch.setattr(winservice, "start_service", lambda name: calls.append("start"))
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    assert daemon_mod.install_service() is True  # verified: the port answers

    assert calls == ["runkey", "register", "stop", "start"]
    assert registered_kwargs["start"] is False  # nothing starts before the port is clear


def test_install_service_linux_stops_a_stray_daemon_but_not_the_units_own(monkeypatch):
    # A dev `python -m palctl.daemon` in a terminal holds the control port and
    # would crash-loop the fresh unit — kill it. The unit's own daemon is
    # systemd's to replace (the restart inside install), never ours to kill.
    import palctl.systemd as systemd

    calls: list[str] = []
    monkeypatch.setattr(daemon_mod.sys, "platform", "linux")
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(
        systemd, "install_service", lambda *a, **k: calls.append("install")
    )
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    monkeypatch.setattr(systemd, "is_active", lambda name: False)  # a stray
    assert daemon_mod.install_service() is True
    assert calls == ["stop", "install"]

    calls.clear()
    monkeypatch.setattr(systemd, "is_active", lambda name: True)  # the unit's own
    assert daemon_mod.install_service() is True
    assert calls == ["install"]


def test_disable_background_startup_removes_both_and_stops_the_daemon(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(daemon_mod, "uninstall_startup", lambda: calls.append("runkey"))
    monkeypatch.setattr(daemon_mod, "uninstall_service", lambda: calls.append("service"))
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    daemon_mod.disable_background_startup()

    assert calls == ["runkey", "service", "stop"]


# ---------------- login-startup daemon replacement ----------------

# start_detached is the login-startup counterpart to the service-reinstall fix:
# any daemon already up is the OLD build/config, so it must be replaced, not
# skipped. Order is the dangerous part — a leftover service registration must
# go before the process is killed, or the service manager resurrects it.


def _startup_env(monkeypatch, *, service: bool, reachable: bool) -> list[str]:
    import subprocess

    import palctl.winservice as winservice

    calls: list[str] = []
    state = {"service": service}
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: state["service"])

    def _uninstall():  # a successful removal — the re-check must see it gone
        calls.append("uninstall")
        state["service"] = False

    monkeypatch.setattr(daemon_mod, "uninstall_service", _uninstall)
    # Before the spawn the port answers (or not) per the scenario; after the
    # spawn the new daemon comes up, so start_detached's verification sees it.
    monkeypatch.setattr(
        daemon_mod, "_daemon_reachable",
        lambda: True if "spawn" in calls else reachable,
    )
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: calls.append("stop"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append("spawn"))
    return calls


def test_start_detached_replaces_running_daemon(monkeypatch):
    calls = _startup_env(monkeypatch, service=True, reachable=True)
    assert daemon_mod.start_detached() is True
    # Service first (its manager would resurrect a killed daemon and fight the
    # new one over the port), then the process, then the fresh spawn.
    assert calls == ["uninstall", "stop", "spawn"]


def test_start_detached_fresh_spawn_touches_nothing(monkeypatch):
    calls = _startup_env(monkeypatch, service=False, reachable=False)
    assert daemon_mod.start_detached() is True
    assert calls == ["spawn"]


def test_start_detached_aborts_when_service_removal_fails(monkeypatch):
    # Unelevated: the service removal fails and it stays registered. Killing
    # the daemon would just get it resurrected by the service manager, and a
    # fresh spawn would lose the port fight to it — report failure (with the
    # admin-prompt fix printed) instead of pretending it worked.
    calls = _startup_env(monkeypatch, service=True, reachable=True)
    monkeypatch.setattr(  # a removal that does NOT clear the registration
        daemon_mod, "uninstall_service", lambda: calls.append("uninstall")
    )
    assert daemon_mod.start_detached() is False
    assert calls == ["uninstall"]  # no kill, no spawn


def test_start_detached_noop_off_windows(monkeypatch):
    calls = _startup_env(monkeypatch, service=True, reachable=True)
    monkeypatch.setattr(daemon_mod.sys, "platform", "linux")
    assert daemon_mod.start_detached() is False
    assert calls == []


def test_start_detached_reports_failure_when_daemon_never_answers(monkeypatch):
    # The spawn is not the success — the control port answering is. A daemon
    # that dies on startup must yield False, not a cheerful "running now".
    calls = _startup_env(monkeypatch, service=False, reachable=False)
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: False)  # never up
    monkeypatch.setattr(  # single-shot wait so the test doesn't sit out the timeout
        daemon_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )
    assert daemon_mod.start_detached() is False
    assert calls == ["spawn"]


def _fake_conn(pid, port, status):
    return types.SimpleNamespace(
        pid=pid, status=status, laddr=types.SimpleNamespace(port=port)
    )


def _stop_daemon_env(monkeypatch):
    import psutil

    from palctl.client import DAEMON_PORT

    listen = psutil.CONN_LISTEN
    conns = [
        _fake_conn(111, DAEMON_PORT, listen),  # the old daemon — must die
        _fake_conn(222, 8212, listen),  # unrelated listener — untouched
        _fake_conn(daemon_mod.os.getpid(), DAEMON_PORT, listen),  # never kill ourselves
        _fake_conn(333, DAEMON_PORT, "ESTABLISHED"),  # a client, not the listener
    ]
    monkeypatch.setattr(psutil, "net_connections", lambda kind="tcp": conns)
    monkeypatch.setattr(psutil, "Process", lambda pid: pid)


def test_stop_daemon_process_kills_only_the_port_listener(monkeypatch):
    from palctl import procs

    _stop_daemon_env(monkeypatch)
    terminated: list[int] = []

    async def fake_terminate(proc, timeout=10.0):
        terminated.append(proc)
        return True

    monkeypatch.setattr(procs, "terminate_process", fake_terminate)
    daemon_mod._stop_daemon_process()
    assert terminated == [111]


def test_stop_daemon_process_escalates_to_kill(monkeypatch):
    from palctl import procs

    _stop_daemon_env(monkeypatch)
    killed: list[int] = []

    async def fake_terminate(proc, timeout=10.0):
        return False  # survived SIGTERM/TerminateProcess

    async def fake_kill(proc, timeout=10.0):
        killed.append(proc)
        return True

    monkeypatch.setattr(procs, "terminate_process", fake_terminate)
    monkeypatch.setattr(procs, "kill_process", fake_kill)
    daemon_mod._stop_daemon_process()
    assert killed == [111]


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
