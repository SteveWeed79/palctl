"""
The Windows scheduled health task — hung-daemon recovery.

The service wrapper restarts a *crashed* daemon, and on Linux systemd's
WatchdogSec also restarts a *wedged* one (alive, but its event loop is stuck —
the failure /healthz reports with a 503). Windows had no wedge coverage at
all: the dashboard could *show* a wedged daemon, but nothing acted on it.

This registers a Task Scheduler job that runs ``palctl-daemon health-check``
every few minutes. The command probes /healthz and, after enough consecutive
failures, restarts the daemon the way it's actually deployed (service or
login-startup process). Service mode registers the task as SYSTEM (it must
restart a service); login mode registers it as the user, which also means it
only runs while they're logged in — exactly when a login-mode daemon exists.

Pure command builders + thin Windows-only runners, like firewall.py.
"""

from __future__ import annotations

import subprocess
import sys

TASK_NAME = "palctl-health"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------- pure command builders ----------------


def create_task_command(
    exe: str, args: str = "", *, every_minutes: int = 5, as_system: bool = False
) -> list[str]:
    """The schtasks invocation that registers the recurring health check.
    /F overwrites an existing registration, so re-install converges instead of
    erroring — same reinstall-replaces rule as the services themselves."""
    run = f'"{exe}"'
    if args:
        run += f" {args}"
    run += " health-check"
    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAME,
        "/TR", run,
        "/SC", "MINUTE",
        "/MO", str(max(1, every_minutes)),
    ]
    if as_system:
        # Restarting a service needs elevation; SYSTEM also runs with nobody
        # logged in — matching when a service-mode daemon exists.
        cmd += ["/RU", "SYSTEM", "/RL", "HIGHEST"]
    return cmd


def delete_task_command() -> list[str]:
    return ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]


def query_task_command() -> list[str]:
    return ["schtasks", "/Query", "/TN", TASK_NAME]


# ---------------- runners (Windows) ----------------


def _on_windows() -> bool:
    return sys.platform.startswith("win")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)


def register_health_task(
    exe: str, args: str = "", *, every_minutes: int = 5, as_system: bool = False
) -> bool:
    """Create (or replace) the health task. False off Windows or on refusal —
    callers treat this as best-effort: a daemon without its healer is still a
    daemon, and the caller logs the outcome."""
    if not _on_windows():
        return False
    try:
        return (
            _run(
                create_task_command(
                    exe, args, every_minutes=every_minutes, as_system=as_system
                )
            ).returncode
            == 0
        )
    except OSError:
        return False


def remove_health_task() -> bool:
    """Delete the health task if present. True when it's gone (or was never
    there); False only on an actual refusal."""
    if not _on_windows():
        return True
    try:
        if _run(query_task_command()).returncode != 0:
            return True  # not registered — nothing to remove
        return _run(delete_task_command()).returncode == 0
    except OSError:
        return False
