"""
Register a service with systemd on Linux — the counterpart to winservice.py's
NSSM on Windows.

The unit-file text is pure and unit-tested. Installing it writes to
/etc/systemd/system and runs systemctl, so it needs root and only does anything
on Linux; that keeps the platform split confined to two small modules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

UNIT_DIR = Path("/etc/systemd/system")


def unit_file(
    name: str,
    exec_start: str,
    *,
    description: str | None = None,
    working_dir: str | None = None,
    user: str | None = None,
) -> str:
    """Render a systemd unit. Restart=on-failure gives the same 'keep it up'
    behaviour NSSM provides on Windows."""
    lines = [
        "[Unit]",
        f"Description={description or name}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=5",
    ]
    if working_dir:
        lines.append(f"WorkingDirectory={working_dir}")
    if user:
        lines.append(f"User={user}")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def install_service(
    name: str,
    exec_start: str,
    *,
    description: str | None = None,
    working_dir: str | None = None,
    user: str | None = None,
    start: bool = True,
) -> None:
    unit_path = UNIT_DIR / f"{name}.service"
    unit_path.write_text(
        unit_file(
            name, exec_start,
            description=description, working_dir=working_dir, user=user,
        ),
        encoding="utf-8",
    )
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", name])
    if start:
        # `systemctl start` is a no-op when the unit is already active, so a
        # re-install over a running daemon would leave the OLD process up with
        # the stale unit/binary. `restart` starts it if stopped and re-launches
        # it if running, so a reinstall actually picks up the rewritten unit.
        _run(["systemctl", "restart", name])


def is_active(name: str) -> bool:
    """Whether the unit is currently active — i.e. the running daemon is
    systemd's to replace on restart, rather than a stray process."""
    return _run(["systemctl", "is-active", name]).stdout.strip() == "active"


def remove_service(name: str) -> None:
    _run(["systemctl", "stop", name])
    _run(["systemctl", "disable", name])
    (UNIT_DIR / f"{name}.service").unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"])
