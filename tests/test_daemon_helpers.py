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


# ---------------- Windows service install: honest permission failures ----------------


def test_install_service_windows_refuses_when_not_elevated(monkeypatch, capsys):
    # The regression this closes: a non-elevated install let sc.exe/WinSW fail
    # silently, waited out the reachability probe, and blamed the daemon. It now
    # refuses up front, touching nothing (no download, no registration).
    import palctl.preflight as preflight
    import palctl.winservice as winservice

    touched: list[str] = []
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: False)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: touched.append("winsw"))
    monkeypatch.setattr(
        winservice, "install_service", lambda *a, **k: touched.append("register")
    )

    assert daemon_mod.install_service() is False
    assert touched == []
    assert "administrator" in capsys.readouterr().out.lower()


def test_install_service_windows_reports_blocked_wrapper_download(monkeypatch, capsys):
    # A proxy/AV/offline box blocking the WinSW download must be reported
    # plainly, not crash with a traceback — and nothing gets registered.
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    registered: list[str] = []
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(
        winservice, "ensure_winsw",
        lambda d: (_ for _ in ()).throw(OSError("proxy blocked github.com")),
    )
    monkeypatch.setattr(
        winservice, "install_service", lambda *a, **k: registered.append("x")
    )

    assert daemon_mod.install_service() is False
    assert registered == []
    out = capsys.readouterr().out.lower()
    assert "download" in out and "github" in out


def test_install_service_windows_surfaces_logon_failure(monkeypatch, capsys):
    # Registered but the SCM won't start it (Error 1069 &c.): surface the real
    # reason read from WIN32_EXIT_CODE, not a generic "not answering".
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(winservice, "install_service", lambda *a, **k: None)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: False)
    monkeypatch.setattr(  # single-shot wait so the test doesn't sit out 30s
        daemon_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )
    monkeypatch.setattr(
        "palctl.procs.service_failure_reason",
        lambda name: "Error 1069: the service's logon account was rejected.",
    )

    assert daemon_mod.install_service() is False
    assert "1069" in capsys.readouterr().out


def test_install_service_windows_reports_registration_blocked(monkeypatch, capsys):
    # The service never registered (WinSW install itself refused): say that,
    # rather than "registered but not answering".
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(winservice, "install_service", lambda *a, **k: None)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    monkeypatch.setattr(winservice, "service_exists", lambda name: False)
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: False)
    monkeypatch.setattr(
        daemon_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )

    assert daemon_mod.install_service() is False
    assert "did not get registered" in capsys.readouterr().out


def test_install_service_windows_as_user_uses_passed_password(monkeypatch):
    # Path A: the setup flow / GUI pass the Windows password in a field, so a
    # user-account service registers non-interactively — never blocking on a
    # getpass prompt the GUI can't answer.
    import getpass

    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    registered: dict = {}
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(
        getpass, "getpass",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted for a password")),
    )

    def fake_install(winsw, name, exe, args, app_dir, **kw):
        registered.update(kw)

    monkeypatch.setattr(winservice, "install_service", fake_install)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    # The --as-user path waits for the service to reach RUNNING (its Error 1069
    # guard); make it so, so we exercise the happy path.
    monkeypatch.setattr("palctl.procs.service_state", lambda name: "RUNNING")
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: None)
    monkeypatch.setenv("USERNAME", "server sw")

    assert daemon_mod.install_service(as_user=True, password="hunter2") is True
    assert registered["user"] == ".\\server sw"
    assert registered["password"] == "hunter2"


def test_uninstall_service_windows_refuses_when_not_elevated(monkeypatch, capsys):
    # Removing a service needs admin too; a refused sc delete must not print
    # "removed" and leave the old registration in place.
    import palctl.preflight as preflight
    import palctl.winservice as winservice

    removed: list[str] = []
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(preflight, "is_elevated", lambda: False)
    monkeypatch.setattr(winservice, "remove_service", lambda name: removed.append(name))

    daemon_mod.uninstall_service()
    assert removed == []
    assert "administrator" in capsys.readouterr().out.lower()


def test_disable_background_startup_removes_both_and_stops_the_daemon(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(daemon_mod, "uninstall_startup", lambda: calls.append("runkey"))
    monkeypatch.setattr(daemon_mod, "uninstall_service", lambda: calls.append("service"))
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    daemon_mod.disable_background_startup()

    assert calls == ["runkey", "service", "stop"]


# ---------------- scheduled health check (hung-daemon recovery) ----------------


def _health_env(monkeypatch, tmp_path, *, probes):
    """Wire run_health_check to canned probe results and a real state file."""
    import palctl.healthcheck as hc

    monkeypatch.setattr(hc, "_STATE_PATH", tmp_path / "health_state.json")
    it = iter(probes)
    monkeypatch.setattr(hc, "probe", lambda timeout=5.0: next(it))
    heals: list[bool] = []

    def _heal():
        heals.append(True)
        return True

    monkeypatch.setattr(daemon_mod, "_heal_daemon", _heal)
    return heals


def test_health_check_heals_only_after_a_confirmed_streak(monkeypatch, tmp_path):
    heals = _health_env(monkeypatch, tmp_path, probes=[False, False, False])
    assert daemon_mod.run_health_check(threshold=3) == 0  # 1/3 — wait
    assert daemon_mod.run_health_check(threshold=3) == 0  # 2/3 — wait
    assert heals == []
    assert daemon_mod.run_health_check(threshold=3) == 0  # 3/3 — heal
    assert heals == [True]


def test_health_check_one_good_probe_resets_the_streak(monkeypatch, tmp_path):
    heals = _health_env(monkeypatch, tmp_path, probes=[False, False, True, False])
    daemon_mod.run_health_check(threshold=3)
    daemon_mod.run_health_check(threshold=3)
    daemon_mod.run_health_check(threshold=3)  # healthy — streak resets
    daemon_mod.run_health_check(threshold=3)  # 1/3 again, not 3/3
    assert heals == []


def test_health_check_failed_heal_exits_nonzero(monkeypatch, tmp_path):
    import palctl.healthcheck as hc

    monkeypatch.setattr(hc, "_STATE_PATH", tmp_path / "health_state.json")
    monkeypatch.setattr(hc, "probe", lambda timeout=5.0: False)
    monkeypatch.setattr(daemon_mod, "_heal_daemon", lambda: False)
    assert daemon_mod.run_health_check(threshold=1) == 1  # visible in task history


def test_heal_daemon_service_mode_stops_clears_port_starts(monkeypatch):
    # The wedged case: SCM stop lands but the process survives holding the
    # port — it must be force-cleared BEFORE the fresh start, or the new
    # daemon loses the port fight and the heal reports failure.
    import palctl.winservice as winservice

    calls: list[str] = []
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(
        "palctl.procs._run_capture", lambda cmd, timeout=30.0: calls.append("sc-stop") or ""
    )
    monkeypatch.setattr("palctl.procs.service_state", lambda name: "STOPPED")
    # Port answers before the kill (the wedged process survived the SCM stop)
    # and after the fresh start (heal verified) — True both times.
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: calls.append("kill"))
    monkeypatch.setattr(winservice, "start_service", lambda name: calls.append("start"))
    monkeypatch.setattr(
        daemon_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )

    assert daemon_mod._heal_daemon() is True
    assert calls == ["sc-stop", "kill", "start"]


def test_install_service_registers_the_health_task(monkeypatch):
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice
    import palctl.wintask as wintask

    registered: dict = {}
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(winservice, "install_service", lambda *a, **k: None)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    monkeypatch.setattr(daemon_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(daemon_mod, "_stop_daemon_process", lambda: None)

    def _register(exe, args="", *, every_minutes=5, as_system=False):
        registered["as_system"] = as_system
        return True

    monkeypatch.setattr(wintask, "register_health_task", _register)

    assert daemon_mod.install_service() is True
    # SYSTEM: a service restart needs elevation, and the healer must run with
    # nobody logged in — matching when a service-mode daemon runs.
    assert registered["as_system"] is True


def test_uninstall_service_removes_the_health_task(monkeypatch):
    import palctl.preflight as preflight
    import palctl.winservice as winservice
    import palctl.wintask as wintask

    removed: list[bool] = []
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(winservice, "remove_service", lambda name: None)
    monkeypatch.setattr("palctl.firewall.remove_rule", lambda: "removed")
    monkeypatch.setattr(wintask, "remove_health_task", lambda: removed.append(True) or True)

    daemon_mod.uninstall_service()
    assert removed == [True]  # no healer left to resurrect a removed daemon


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
    assert calls == [("firewall", "0.0.0.0"), ("mirror",)]
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


# ---------------- a stop palctl didn't make is still a stop ----------------
#
# Auto-recovery decided from one signal — "the REST API stopped answering" —
# which cannot tell a crash from an admin stopping the service. palctl only knew
# a stop was deliberate when it did the stopping, so every other stop read as a
# crash and got undone within seconds. The service could not be turned off by
# any normal means.


def test_a_deliberate_stop_is_recognised():
    assert daemon_mod.looks_externally_stopped(
        service_state="STOPPED", busy=False, restarting=False
    )


def test_a_crashed_server_is_not_mistaken_for_a_deliberate_stop():
    """The distinction the SCM already knows: a crash leaves the service
    RUNNING (or START_PENDING while the wrapper restarts it) with nothing
    answering behind it. Only a stop request produces STOPPED."""
    for state in ("RUNNING", "START_PENDING", "STOP_PENDING", "UNKNOWN"):
        assert not daemon_mod.looks_externally_stopped(
            service_state=state, busy=False, restarting=False
        ), state


def test_palctls_own_operations_are_not_mistaken_for_an_admin():
    """Every restart palctl runs passes through STOPPED on its way back up."""
    assert not daemon_mod.looks_externally_stopped(
        service_state="STOPPED", busy=True, restarting=False
    )
    assert not daemon_mod.looks_externally_stopped(
        service_state="STOPPED", busy=False, restarting=True
    )


def _daemon_for_external_stop(service_state: str, *, desired=True):
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d.__dict__["_Daemon__desired_running"] = desired
    d._external_stop_polls = 0
    d._down_polls = 0
    d.emitted = []
    d.control = types.SimpleNamespace(busy=False)
    d.watchdog = types.SimpleNamespace(is_restarting=False)

    async def _state():
        return service_state

    d._service_state_cached = _state

    class _Bus:
        @staticmethod
        async def emit(e):
            d.emitted.append(e)

    d.bus = _Bus()
    return d


def _adopted(d):
    return [e for e in d.emitted if e.data.get("action") == "external_stop"]


def test_an_external_stop_is_adopted_only_after_confirmation(monkeypatch, tmp_path):
    """A restart passes through STOPPED for a moment; sampling one of those must
    not be read as 'they want it off'."""
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")
    d = _daemon_for_external_stop("STOPPED")

    for _ in range(daemon_mod.EXTERNAL_STOP_CONFIRM_POLLS - 1):
        assert asyncio.run(d._adopt_external_stop()) is True  # skip recovery...
        assert _adopted(d) == []                              # ...but not decided yet
        assert d._desired_running is True

    assert asyncio.run(d._adopt_external_stop()) is True
    assert len(_adopted(d)) == 1
    assert d._desired_running is False, "the stop must be recorded, not just skipped"


def test_a_running_service_lets_recovery_proceed(monkeypatch, tmp_path):
    """The genuine-crash path has to keep working — that's the whole feature."""
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")
    d = _daemon_for_external_stop("RUNNING")
    assert asyncio.run(d._adopt_external_stop()) is False
    assert d._desired_running is True


def test_a_brief_stopped_blip_does_not_stick(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon_mod, "_STATE_PATH", tmp_path / "daemon_state.json")
    d = _daemon_for_external_stop("STOPPED")
    asyncio.run(d._adopt_external_stop())
    assert d._external_stop_polls == 1

    async def _running():
        return "RUNNING"

    d._service_state_cached = _running
    assert asyncio.run(d._adopt_external_stop()) is False
    assert d._external_stop_polls == 0, "the streak must reset when it comes back"

