"""The headless Linux path: a systemd unit for the game server, and a setup
command that doesn't need Qt.

The README has always advertised headless Linux. Two things made that untrue:
`setup_flow.run_setup` is deliberately Qt-free and had no caller but the
wizard, and `_register_server_service` keyed off `PalServer.exe`, so on Linux
setup registered nothing and every later `systemctl start <service_name>` went
at a unit that did not exist.
"""

from __future__ import annotations

from palctl import setup_flow
from palctl.systemd import server_unit_file, unit_file


def sections(text: str) -> dict[str, dict[str, str]]:
    """Parse a unit into {section: {key: value}} so assertions are about
    settings rather than about string positions."""
    out: dict[str, dict[str, str]] = {}
    current = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out.setdefault(current, {})
        elif "=" in line and current:
            key, _, value = line.partition("=")
            out[current][key] = value
    return out


# ---------------- the game server's unit ----------------


def test_the_server_unit_is_not_a_notify_unit():
    """PalServer never calls sd_notify. Under Type=notify systemd waits for a
    READY it will never get, reports `activating` for the full 90-second
    default, then kills a server that was running perfectly the whole time."""
    svc = sections(server_unit_file("palserver", "/srv/pal"))["Service"]

    assert svc["Type"] == "simple"
    assert "WatchdogSec" not in svc
    assert "NotifyAccess" not in svc


def test_the_daemon_unit_is_still_a_notify_unit():
    """Guard: the two units must not converge. The daemon's watchdog is what
    catches a wedged event loop, and it depends on Type=notify."""
    svc = sections(unit_file("palctl-daemon", "/usr/bin/palctl-daemon"))["Service"]

    assert svc["Type"] == "notify"
    assert svc["WatchdogSec"] == "120"


def test_the_server_unit_always_has_a_working_directory():
    """The launcher resolves its engine binary and Pal/ tree relative to the
    current directory; started from / it exits immediately."""
    svc = sections(server_unit_file("palserver", "/srv/pal"))["Service"]

    assert svc["WorkingDirectory"] == "/srv/pal"
    assert svc["ExecStart"] == "/srv/pal/PalServer.sh"


def test_a_trailing_slash_does_not_double_up():
    svc = sections(server_unit_file("palserver", "/srv/pal/"))["Service"]
    assert svc["ExecStart"] == "/srv/pal/PalServer.sh"


def test_the_server_gets_longer_to_stop_than_systemds_default():
    """The world is written on shutdown, and SIGKILL at the default 90s can cut
    that short — which is a corrupt save on every unlucky reboot."""
    svc = sections(server_unit_file("palserver", "/srv/pal"))["Service"]

    assert int(svc["TimeoutStopSec"]) >= 120
    assert svc["KillSignal"] == "SIGINT"


def test_the_server_waits_before_restarting():
    """Restarting after five seconds fights the server's own shutdown — it
    needs time to release its UDP port and finish writing."""
    svc = sections(server_unit_file("palserver", "/srv/pal"))["Service"]

    assert int(svc["RestartSec"]) >= 20
    assert svc["Restart"] == "on-failure"


def test_the_crash_loop_guard_is_in_the_unit_section():
    """StartLimit* moved to [Unit] in systemd 229 and is silently ignored under
    [Service] on anything newer — the same trap the daemon's unit documents."""
    parsed = sections(server_unit_file("palserver", "/srv/pal"))

    assert parsed["Unit"]["StartLimitBurst"] == "5"
    assert "StartLimitBurst" not in parsed["Service"]


def test_the_unit_can_run_as_a_named_user():
    svc = sections(server_unit_file("palserver", "/srv/pal", user="steve"))["Service"]
    assert svc["User"] == "steve"


def test_a_custom_launcher_is_honoured():
    svc = sections(
        server_unit_file("palserver", "/srv/pal", launcher="start.sh")
    )["Service"]
    assert svc["ExecStart"] == "/srv/pal/start.sh"


# ---------------- registering it ----------------


def test_registering_the_server_unit_does_not_enable_it(tmp_path, monkeypatch):
    """palctl's daemon owns the boot decision — it starts the server at boot
    only if that is how the operator left it. An enabled unit would have
    systemd start it behind the daemon's back, which is the Linux version of
    the Windows boot-ownership bug."""
    from palctl import systemd

    monkeypatch.setattr(systemd, "UNIT_DIR", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(systemd, "_run", lambda cmd: calls.append(cmd))

    systemd.install_server_service("palserver", "/srv/pal")

    assert (tmp_path / "palserver.service").is_file()
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "disable", "palserver"] in calls
    assert ["systemctl", "enable", "palserver"] not in calls


def test_enabling_is_available_when_explicitly_asked_for(tmp_path, monkeypatch):
    from palctl import systemd

    monkeypatch.setattr(systemd, "UNIT_DIR", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(systemd, "_run", lambda cmd: calls.append(cmd))

    systemd.install_server_service("palserver", "/srv/pal", enable=True)

    assert ["systemctl", "enable", "palserver"] in calls


def test_setup_skips_the_unit_when_the_launcher_is_missing(tmp_path):
    """Registering a unit whose ExecStart does not exist produces a service
    that fails on every start with no explanation."""
    logged: list[str] = []
    plan = setup_flow.SetupPlan(
        server_root=str(tmp_path), steamcmd_path="", api_port=8212, password="x",
        install_server=False, install_vcredist=False, register_server_service=True,
        daemon_startup="none", service_name="palserver",
    )

    assert not setup_flow._register_server_unit_linux(plan, logged.append)
    assert any("not found" in line for line in logged)


def test_setup_registers_the_unit_when_the_launcher_is_there(tmp_path, monkeypatch):
    (tmp_path / "PalServer.sh").write_text("#!/bin/sh\n")
    registered: list[tuple] = []
    monkeypatch.setattr(
        setup_flow, "_invoking_username", lambda: "steve", raising=False
    )
    from palctl import systemd

    monkeypatch.setattr(
        systemd, "install_server_service",
        lambda name, root, **kw: registered.append((name, root, kw)),
    )
    logged: list[str] = []
    plan = setup_flow.SetupPlan(
        server_root=str(tmp_path), steamcmd_path="", api_port=8212, password="x",
        install_server=False, install_vcredist=False, register_server_service=True,
        daemon_startup="none", service_name="palserver",
    )

    assert setup_flow._register_server_unit_linux(plan, logged.append)
    assert registered[0][0] == "palserver"
    assert registered[0][2]["user"] == "steve"


def test_a_permission_failure_is_reported_with_the_remedy(tmp_path, monkeypatch):
    """Writing to /etc/systemd/system needs root, and "Permission denied" alone
    does not tell an operator to re-run with sudo."""
    (tmp_path / "PalServer.sh").write_text("#!/bin/sh\n")
    from palctl import systemd

    def denied(*a, **k):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(systemd, "install_server_service", denied)
    logged: list[str] = []
    plan = setup_flow.SetupPlan(
        server_root=str(tmp_path), steamcmd_path="", api_port=8212, password="x",
        install_server=False, install_vcredist=False, register_server_service=True,
        daemon_startup="none", service_name="palserver",
    )

    assert not setup_flow._register_server_unit_linux(plan, logged.append)
    assert any("sudo" in line for line in logged)


# ---------------- which build is installed ----------------


def test_a_windows_install_is_detected_by_its_launcher(tmp_path):
    """Keyed on the launcher, not sys.platform — which is what keeps the
    Windows registration path reachable from a suite running on Linux."""
    (tmp_path / "PalServer.exe").write_text("MZ")
    assert setup_flow._server_flavour(str(tmp_path)) == "windows"


def test_a_linux_install_is_detected_by_its_launcher(tmp_path):
    (tmp_path / "PalServer.sh").write_text("#!/bin/sh\n")
    assert setup_flow._server_flavour(str(tmp_path)) == "linux"


def test_an_empty_directory_falls_back_to_this_platform(tmp_path):
    """So the "install the server first" message still comes from the branch
    that can act on it."""
    import sys

    expected = "windows" if sys.platform.startswith("win") else "linux"
    assert setup_flow._server_flavour(str(tmp_path)) == expected


def test_a_windows_server_directory_wins_over_the_host_platform(tmp_path):
    """A Windows server tree mounted on a Linux box still needs the WinSW
    branch's diagnosis, not a systemd unit that could never start it."""
    (tmp_path / "PalServer.exe").write_text("MZ")
    (tmp_path / "PalServer.sh").write_text("#!/bin/sh\n")

    assert setup_flow._server_flavour(str(tmp_path)) == "windows"
