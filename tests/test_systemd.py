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
