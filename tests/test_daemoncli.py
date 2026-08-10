"""The daemon's lifecycle CLI: install, start, heal, remove.

Split out of test_daemon_helpers.py alongside the module split — this is the
program that runs once and exits (registering services, replacing a running
daemon, the scheduled health check), not the one that runs for weeks. Its
failures are the silent kind: a service that registers but can't start, a
"successful" install that left the old daemon holding the port, a health check
that restarts a daemon that was fine.

These patch names on `palctl.daemoncli` itself, which is where the code lives.
`palctl.daemon` re-exports every one of them, and that re-export is what the
setup flow and the GUI call.
"""

import types

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("discord")

import palctl.daemoncli as cli_mod  # noqa: E402
from palctl.daemoncli import service_target  # noqa: E402


def test_the_runtime_names_stay_importable_from_palctl_daemon():
    """The module split must not move the API. setup_flow, the wizard and the
    console entry point all call these as `daemon.<name>`."""
    import palctl.daemon as daemon_mod

    for name in (
        "SERVICE_NAME", "install_service", "uninstall_service", "install_startup",
        "uninstall_startup", "disable_background_startup", "start_detached",
        "run_health_check", "service_target", "main",
    ):
        assert getattr(daemon_mod, name) is getattr(cli_mod, name), name


# ---------------- frozen service target ----------------


def test_service_target_frozen_resolves_daemon_exe_from_gui(tmp_path, monkeypatch):
    # The onedir frozen build ships palctl-daemon.exe and palctl-gui.exe side by
    # side. The wizard registers the daemon service from inside the GUI process,
    # so sys.executable is the GUI — but the service must still point at the
    # DAEMON exe, or the daemon never runs and every GUI action hits 10061.
    (tmp_path / "palctl-daemon.exe").write_bytes(b"MZ")
    gui = tmp_path / "palctl-gui.exe"
    gui.write_bytes(b"MZ")

    monkeypatch.setattr(cli_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli_mod.sys, "executable", str(gui))
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")

    exe, args, app_dir = service_target()
    assert exe.endswith("palctl-daemon.exe")
    assert args == ""
    assert app_dir == str(tmp_path)


def test_service_target_frozen_falls_back_when_daemon_exe_absent(tmp_path, monkeypatch):
    # Odd layout (no sibling daemon exe): register the running exe rather than a
    # path that doesn't exist.
    gui = tmp_path / "palctl-gui.exe"
    gui.write_bytes(b"MZ")

    monkeypatch.setattr(cli_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli_mod.sys, "executable", str(gui))
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")

    exe, _, _ = service_target()
    assert exe == str(gui)


def test_service_target_dev_uses_module_invocation(monkeypatch):
    monkeypatch.setattr(cli_mod.sys, "frozen", False, raising=False)
    exe, args, _ = service_target()
    assert args == "-m palctl.daemon"


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
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: calls.append("runkey"))
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")

    def fake_install(winsw, name, exe, args, app_dir, **kw):
        registered_kwargs.update(kw)
        calls.append("register")

    monkeypatch.setattr(winservice, "install_service", fake_install)
    monkeypatch.setattr(winservice, "start_service", lambda name: calls.append("start"))
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    assert cli_mod.install_service() is True  # verified: the port answers

    assert calls == ["runkey", "register", "stop", "start"]
    assert registered_kwargs["start"] is False  # nothing starts before the port is clear


def test_install_service_linux_stops_a_stray_daemon_but_not_the_units_own(monkeypatch):
    # A dev `python -m palctl.daemon` in a terminal holds the control port and
    # would crash-loop the fresh unit — kill it. The unit's own daemon is
    # systemd's to replace (the restart inside install), never ours to kill.
    import palctl.systemd as systemd

    calls: list[str] = []
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(
        systemd, "install_service", lambda *a, **k: calls.append("install")
    )
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    monkeypatch.setattr(systemd, "is_active", lambda name: False)  # a stray
    assert cli_mod.install_service() is True
    assert calls == ["stop", "install"]

    calls.clear()
    monkeypatch.setattr(systemd, "is_active", lambda name: True)  # the unit's own
    assert cli_mod.install_service() is True
    assert calls == ["install"]


# ---------------- Windows service install: honest permission failures ----------------


def test_install_service_windows_refuses_when_not_elevated(monkeypatch, capsys):
    # The regression this closes: a non-elevated install let sc.exe/WinSW fail
    # silently, waited out the reachability probe, and blamed the daemon. It now
    # refuses up front, touching nothing (no download, no registration).
    import palctl.preflight as preflight
    import palctl.winservice as winservice

    touched: list[str] = []
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: False)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: touched.append("winsw"))
    monkeypatch.setattr(
        winservice, "install_service", lambda *a, **k: touched.append("register")
    )

    assert cli_mod.install_service() is False
    assert touched == []
    assert "administrator" in capsys.readouterr().out.lower()


def test_install_service_windows_reports_blocked_wrapper_download(monkeypatch, capsys):
    # A proxy/AV/offline box blocking the WinSW download must be reported
    # plainly, not crash with a traceback — and nothing gets registered.
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    registered: list[str] = []
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(
        winservice, "ensure_winsw",
        lambda d: (_ for _ in ()).throw(OSError("proxy blocked github.com")),
    )
    monkeypatch.setattr(
        winservice, "install_service", lambda *a, **k: registered.append("x")
    )

    assert cli_mod.install_service() is False
    assert registered == []
    out = capsys.readouterr().out.lower()
    assert "download" in out and "github" in out


def test_install_service_windows_surfaces_logon_failure(monkeypatch, capsys):
    # Registered but the SCM won't start it (Error 1069 &c.): surface the real
    # reason read from WIN32_EXIT_CODE, not a generic "not answering".
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(winservice, "install_service", lambda *a, **k: None)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: False)
    monkeypatch.setattr(  # single-shot wait so the test doesn't sit out 30s
        cli_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )
    monkeypatch.setattr(
        "palctl.procs.service_failure_reason",
        lambda name: "Error 1069: the service's logon account was rejected.",
    )

    assert cli_mod.install_service() is False
    assert "1069" in capsys.readouterr().out


def test_install_service_windows_reports_registration_blocked(monkeypatch, capsys):
    # The service never registered (WinSW install itself refused): say that,
    # rather than "registered but not answering".
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice

    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(winservice, "install_service", lambda *a, **k: None)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    monkeypatch.setattr(winservice, "service_exists", lambda name: False)
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: False)
    monkeypatch.setattr(
        cli_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )

    assert cli_mod.install_service() is False
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
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
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
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: None)
    monkeypatch.setenv("USERNAME", "server sw")

    assert cli_mod.install_service(as_user=True, password="hunter2") is True
    assert registered["user"] == ".\\server sw"
    assert registered["password"] == "hunter2"


def test_uninstall_service_windows_refuses_when_not_elevated(monkeypatch, capsys):
    # Removing a service needs admin too; a refused sc delete must not print
    # "removed" and leave the old registration in place.
    import palctl.preflight as preflight
    import palctl.winservice as winservice

    removed: list[str] = []
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(preflight, "is_elevated", lambda: False)
    monkeypatch.setattr(winservice, "remove_service", lambda name: removed.append(name))

    cli_mod.uninstall_service()
    assert removed == []
    assert "administrator" in capsys.readouterr().out.lower()


def test_disable_background_startup_removes_both_and_stops_the_daemon(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli_mod, "uninstall_startup", lambda: calls.append("runkey"))
    monkeypatch.setattr(cli_mod, "uninstall_service", lambda: calls.append("service"))
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: calls.append("stop"))

    cli_mod.disable_background_startup()

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

    monkeypatch.setattr(cli_mod, "_heal_daemon", _heal)
    return heals


def test_health_check_heals_only_after_a_confirmed_streak(monkeypatch, tmp_path):
    heals = _health_env(monkeypatch, tmp_path, probes=[False, False, False])
    assert cli_mod.run_health_check(threshold=3) == 0  # 1/3 — wait
    assert cli_mod.run_health_check(threshold=3) == 0  # 2/3 — wait
    assert heals == []
    assert cli_mod.run_health_check(threshold=3) == 0  # 3/3 — heal
    assert heals == [True]


def test_health_check_one_good_probe_resets_the_streak(monkeypatch, tmp_path):
    heals = _health_env(monkeypatch, tmp_path, probes=[False, False, True, False])
    cli_mod.run_health_check(threshold=3)
    cli_mod.run_health_check(threshold=3)
    cli_mod.run_health_check(threshold=3)  # healthy — streak resets
    cli_mod.run_health_check(threshold=3)  # 1/3 again, not 3/3
    assert heals == []


def test_health_check_failed_heal_exits_nonzero(monkeypatch, tmp_path):
    import palctl.healthcheck as hc

    monkeypatch.setattr(hc, "_STATE_PATH", tmp_path / "health_state.json")
    monkeypatch.setattr(hc, "probe", lambda timeout=5.0: False)
    monkeypatch.setattr(cli_mod, "_heal_daemon", lambda: False)
    assert cli_mod.run_health_check(threshold=1) == 1  # visible in task history


def test_heal_daemon_service_mode_stops_clears_port_starts(monkeypatch):
    # The wedged case: SCM stop lands but the process survives holding the
    # port — it must be force-cleared BEFORE the fresh start, or the new
    # daemon loses the port fight and the heal reports failure.
    import palctl.winservice as winservice

    calls: list[str] = []
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(
        "palctl.procs._run_capture", lambda cmd, timeout=30.0: calls.append("sc-stop") or ""
    )
    monkeypatch.setattr("palctl.procs.service_state", lambda name: "STOPPED")
    # Port answers before the kill (the wedged process survived the SCM stop)
    # and after the fresh start (heal verified) — True both times.
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: calls.append("kill"))
    monkeypatch.setattr(winservice, "start_service", lambda name: calls.append("start"))
    monkeypatch.setattr(
        cli_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )

    assert cli_mod._heal_daemon() is True
    assert calls == ["sc-stop", "kill", "start"]


def test_install_service_registers_the_health_task(monkeypatch):
    import palctl.preflight as preflight
    import palctl.startup as startup_mod
    import palctl.winservice as winservice
    import palctl.wintask as wintask

    registered: dict = {}
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(startup_mod, "uninstall_startup", lambda: None)
    monkeypatch.setattr(winservice, "ensure_winsw", lambda d: "winsw.exe")
    monkeypatch.setattr(winservice, "install_service", lambda *a, **k: None)
    monkeypatch.setattr(winservice, "start_service", lambda name: None)
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: True)
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: None)

    def _register(exe, args="", *, every_minutes=5, as_system=False):
        registered["as_system"] = as_system
        return True

    monkeypatch.setattr(wintask, "register_health_task", _register)

    assert cli_mod.install_service() is True
    # SYSTEM: a service restart needs elevation, and the healer must run with
    # nobody logged in — matching when a service-mode daemon runs.
    assert registered["as_system"] is True


def test_uninstall_service_removes_the_health_task(monkeypatch):
    import palctl.preflight as preflight
    import palctl.winservice as winservice
    import palctl.wintask as wintask

    removed: list[bool] = []
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    monkeypatch.setattr(preflight, "is_elevated", lambda: True)
    monkeypatch.setattr(winservice, "remove_service", lambda name: None)
    monkeypatch.setattr("palctl.firewall.remove_rule", lambda: "removed")
    monkeypatch.setattr(wintask, "remove_health_task", lambda: removed.append(True) or True)

    cli_mod.uninstall_service()
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
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(winservice, "service_exists", lambda name: state["service"])

    def _uninstall():  # a successful removal — the re-check must see it gone
        calls.append("uninstall")
        state["service"] = False

    monkeypatch.setattr(cli_mod, "uninstall_service", _uninstall)
    # Before the spawn the port answers (or not) per the scenario; after the
    # spawn the new daemon comes up, so start_detached's verification sees it.
    monkeypatch.setattr(
        cli_mod, "_daemon_reachable",
        lambda: True if "spawn" in calls else reachable,
    )
    monkeypatch.setattr(cli_mod, "_stop_daemon_process", lambda: calls.append("stop"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append("spawn"))
    return calls


def test_start_detached_replaces_running_daemon(monkeypatch):
    calls = _startup_env(monkeypatch, service=True, reachable=True)
    assert cli_mod.start_detached() is True
    # Service first (its manager would resurrect a killed daemon and fight the
    # new one over the port), then the process, then the fresh spawn.
    assert calls == ["uninstall", "stop", "spawn"]


def test_start_detached_fresh_spawn_touches_nothing(monkeypatch):
    calls = _startup_env(monkeypatch, service=False, reachable=False)
    assert cli_mod.start_detached() is True
    assert calls == ["spawn"]


def test_start_detached_aborts_when_service_removal_fails(monkeypatch):
    # Unelevated: the service removal fails and it stays registered. Killing
    # the daemon would just get it resurrected by the service manager, and a
    # fresh spawn would lose the port fight to it — report failure (with the
    # admin-prompt fix printed) instead of pretending it worked.
    calls = _startup_env(monkeypatch, service=True, reachable=True)
    monkeypatch.setattr(  # a removal that does NOT clear the registration
        cli_mod, "uninstall_service", lambda: calls.append("uninstall")
    )
    assert cli_mod.start_detached() is False
    assert calls == ["uninstall"]  # no kill, no spawn


def test_start_detached_noop_off_windows(monkeypatch):
    calls = _startup_env(monkeypatch, service=True, reachable=True)
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    assert cli_mod.start_detached() is False
    assert calls == []


def test_start_detached_reports_failure_when_daemon_never_answers(monkeypatch):
    # The spawn is not the success — the control port answering is. A daemon
    # that dies on startup must yield False, not a cheerful "running now".
    calls = _startup_env(monkeypatch, service=False, reachable=False)
    monkeypatch.setattr(cli_mod, "_daemon_reachable", lambda: False)  # never up
    monkeypatch.setattr(  # single-shot wait so the test doesn't sit out the timeout
        cli_mod, "_wait_until", lambda pred, timeout, interval=1.0: pred()
    )
    assert cli_mod.start_detached() is False
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
        _fake_conn(cli_mod.os.getpid(), DAEMON_PORT, listen),  # never kill ourselves
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
    cli_mod._stop_daemon_process()
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
    cli_mod._stop_daemon_process()
    assert killed == [111]


