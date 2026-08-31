"""
Register a service with systemd on Linux — the counterpart to winservice.py's
WinSW on Windows.

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
    behaviour the WinSW wrapper provides on Windows.

    Type=notify + WatchdogSec closes the gap on-failure can't: a daemon whose
    event loop has *wedged* while the process stays alive. The daemon sends
    READY=1 once it's serving and WATCHDOG=1 every half-interval (see
    daemon.sd_notify / _liveness_loop); if the pings stop, systemd restarts it.
    on-failure still covers real crashes.

    StartLimit* is the other half of that, and without it Restart=on-failure is
    a trap. Some startup failures are permanent: another daemon already holds
    the control port (daemon.run logs it and re-raises), a config directory that
    can't be written, a Python environment broken by a half-finished upgrade.
    systemd would then restart the daemon every RestartSec forever — and at one
    restart per 5s it stays *under* systemd's own default rate limiter (5 starts
    per 10s), so the unit never reaches `failed` and never appears in
    `systemctl --failed`. An operator sees a service that claims to be
    activating, a game server nobody is supervising, and no clue why. A loop
    that cannot succeed is not "keeping the daemon up"; it is hiding the reason
    it is down.

    Five failures inside five minutes is a crash loop by any reading, so the
    unit stops and says so. A daemon that stays up longer than that clears the
    count on its own, so a genuinely transient crash still recovers untouched.
    The cause is already in the rotating file log (daemon.run and daemoncli.main
    both write it there before exiting), so `systemctl status` plus that log is
    enough to act on."""
    lines = [
        "[Unit]",
        f"Description={description or name}",
        "After=network.target",
        # These two are [Unit] options, not [Service] ones — they moved in
        # systemd 229 and are silently ignored under [Service] on anything newer.
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=notify",
        "NotifyAccess=main",
        "WatchdogSec=120",
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


# systemctl blocks on the systemd job it queues; a unit whose start or stop
# jobs are stuck holds the call open indefinitely. Bounded so installing or
# removing palctl's own service can't hang the CLI with no way out.
SYSTEMCTL_TIMEOUT = 60.0


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a systemctl command, bounded. A timeout is reported as a non-zero
    result rather than an exception, so callers checking `returncode` keep
    working — TimeoutExpired is a SubprocessError, not an OSError."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="",
            stderr=f"systemctl timed out after {SYSTEMCTL_TIMEOUT:.0f}s",
        )


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
