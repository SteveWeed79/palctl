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


def test_unit_file_omits_optional_fields():
    u = systemd.unit_file("svc", "/bin/true")
    assert "WorkingDirectory=" not in u
    assert "User=" not in u
    assert "Description=svc" in u  # falls back to the name
