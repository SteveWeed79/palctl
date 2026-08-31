"""The systemd unit is the Linux equivalent of the NSSM registration, so the
generated file is pinned: it must run the right command, restart on failure, and
enable at boot."""

from palctl import systemd


def test_unit_file_has_required_sections():
    u = systemd.unit_file(
        "palctl-daemon", "/usr/bin/python3 -m palctl.daemon",
        description="palctl daemon", working_dir="/opt/palctl", user="pal",
    )
    assert "[Unit]" in u and "[Service]" in u and "[Install]" in u
    assert "ExecStart=/usr/bin/python3 -m palctl.daemon" in u
    assert "Description=palctl daemon" in u
    assert "WorkingDirectory=/opt/palctl" in u
    assert "User=pal" in u
    assert "Restart=on-failure" in u
    assert "WantedBy=multi-user.target" in u
    # Type=notify + WatchdogSec: systemd restarts a daemon whose event loop
    # wedged (pings stop) — the failure Restart=on-failure can't see.
    assert "Type=notify" in u
    assert "WatchdogSec=" in u


def test_unit_file_omits_optional_fields():
    u = systemd.unit_file("svc", "/bin/true")
    assert "WorkingDirectory=" not in u
    assert "User=" not in u
    assert "Description=svc" in u  # falls back to the name


def test_install_restarts_so_reinstall_picks_up_new_unit(tmp_path, monkeypatch):
    # A re-install over a RUNNING daemon must re-launch it, or the old process
    # keeps the stale unit/binary. `systemctl start` no-ops on an active unit,
    # so install must use `restart` after reloading.
    calls: list[list[str]] = []
    monkeypatch.setattr(systemd, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(systemd, "UNIT_DIR", tmp_path)

    systemd.install_service("palctl-daemon", "/usr/bin/python3 -m palctl.daemon")

    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "restart", "palctl-daemon"] in calls
    assert ["systemctl", "start", "palctl-daemon"] not in calls


def test_is_active_parses_systemctl_output(monkeypatch):
    import types

    monkeypatch.setattr(
        systemd, "_run", lambda cmd: types.SimpleNamespace(stdout="active\n")
    )
    assert systemd.is_active("svc") is True
    monkeypatch.setattr(
        systemd, "_run", lambda cmd: types.SimpleNamespace(stdout="inactive\n")
    )
    assert systemd.is_active("svc") is False


def test_install_without_start_does_not_touch_the_running_unit(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(systemd, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(systemd, "UNIT_DIR", tmp_path)

    systemd.install_service("svc", "/bin/true", start=False)

    assert not any(c[:2] == ["systemctl", "restart"] for c in calls)
    assert not any(c[:2] == ["systemctl", "start"] for c in calls)


def test_a_hung_systemctl_reads_as_failure_not_an_exception(monkeypatch):
    """systemctl blocks on the job it queues; a unit with a stuck start/stop job
    holds the call open indefinitely. A timeout has to surface as a non-zero
    result, since callers check returncode and never catch SubprocessError."""
    import subprocess

    def fake_run(cmd, **kwargs):
        assert kwargs.get("timeout") == systemd.SYSTEMCTL_TIMEOUT
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert systemd._run(["systemctl", "start", "palctl"]).returncode != 0


# ---------- the restart policy must be able to give up ----------


def _section(unit: str, name: str) -> str:
    """The body of one [Section] of a unit file. Placement matters here: the
    StartLimit* directives moved to [Unit] in systemd 229 and are silently
    ignored under [Service] on anything newer, so a test that only greps the
    whole file would pass on a unit where they do nothing."""
    blocks = unit.split("\n[")
    for b in blocks:
        head = b.lstrip("[").split("]", 1)
        if head[0] == name:
            return head[1] if len(head) > 1 else ""
    raise AssertionError(f"no [{name}] section in:\n{unit}")


def test_a_daemon_that_can_never_start_stops_instead_of_looping_forever():
    """Restart=on-failure alone is a trap. Some startup failures are permanent —
    another daemon already holds the control port, an unwritable config dir, a
    half-upgraded environment — and RestartSec=5 then restarts forever. At one
    restart per 5s that stays UNDER systemd's own default limiter (5 per 10s),
    so the unit never reaches `failed`, never shows in `systemctl --failed`, and
    the operator sees a service that looks like it is activating while nothing
    supervises their server."""
    unit = systemd.unit_file("palctl-daemon", "/usr/bin/python3 -m palctl.daemon")
    u = _section(unit, "Unit")
    assert "StartLimitIntervalSec=" in u, "no start rate limit — restarts forever"
    assert "StartLimitBurst=" in u
    # ...and the window must actually bite at RestartSec=5: the burst has to be
    # reachable inside the interval, or the limiter never trips.
    interval = int(u.split("StartLimitIntervalSec=")[1].split("\n")[0])
    burst = int(u.split("StartLimitBurst=")[1].split("\n")[0])
    restart_sec = int(unit.split("RestartSec=")[1].split("\n")[0])
    assert burst * restart_sec < interval, (
        f"{burst} restarts at {restart_sec}s each cannot fit in {interval}s — "
        "the limiter would never trip"
    )
    # A daemon that stays up past the window clears its count, so an occasional
    # transient crash is not walked toward give-up.
    assert interval >= 60


def test_start_limit_directives_are_not_hidden_in_the_service_section():
    """They are [Unit] options since systemd 229; under [Service] they parse as
    unknown and do nothing, which looks identical to this fix being present."""
    unit = systemd.unit_file("palctl-daemon", "/usr/bin/true")
    assert "StartLimit" not in _section(unit, "Service")
