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


def task_run_string(exe: str, args: str = "", app_dir: str | None = None) -> str:
    """The command Task Scheduler will run, as its /TR value.

    `app_dir` exists because ``schtasks /Create`` has no working-directory
    option at all — that lives in the task XML, not on the command line — and a
    task with no working directory runs from the scheduler's own (System32).
    That is fine for the frozen build, whose /TR is an absolute path to
    palctl-daemon.exe with no arguments. It is fatal for a source install, where
    service_target() yields ``python.exe -m palctl.daemon``: ``-m`` resolves
    against the current directory and sys.path, so the task failed with "No
    module named palctl" every five minutes, forever. Nothing noticed —
    register_health_task returned True because schtasks had registered the task
    perfectly well; it was the task that could not run. Source installs on
    Windows therefore had no wedged-daemon recovery at all while being told they
    did.

    So when a working directory is needed, the command is wrapped in ``cmd /c cd
    /d <dir> && …``. The frozen path is left exactly as it was — it works today,
    it is what the installer ships, and it is not worth putting a shell in front
    of the majority case to fix the minority one.
    """
    run = f'"{exe}"'
    if args:
        run += f" {args}"
    run += " health-check"
    if app_dir:
        # /d so it changes drive as well as directory; cmd exits with the
        # command's own status, so the scheduler still records real failures.
        run = f'cmd /c cd /d "{app_dir}" && {run}'
    return run


def create_task_command(
    exe: str,
    args: str = "",
    *,
    every_minutes: int = 5,
    as_system: bool = False,
    app_dir: str | None = None,
) -> list[str]:
    """The schtasks invocation that registers the recurring health check.
    /F overwrites an existing registration, so re-install converges instead of
    erroring — same reinstall-replaces rule as the services themselves."""
    run = task_run_string(exe, args, app_dir)
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


# schtasks talks to the Task Scheduler service; when that service is wedged the
# call can hang indefinitely. This runs during install and from the setup
# wizard, so an unbounded wait is a stuck installer with no output. Every caller
# already treats failure as best-effort ("a daemon without its healer is still a
# daemon"), so a timeout folds into the existing False/failed path.
SCHTASKS_TIMEOUT = 30.0


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a schtasks command, bounded. A timeout comes back as a non-zero
    result rather than an exception, so it lands in the same "couldn't do it"
    branch every caller already has — TimeoutExpired is a SubprocessError, not
    an OSError, so it would otherwise sail straight past their except clauses."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, creationflags=_NO_WINDOW,
            timeout=SCHTASKS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="",
            stderr=f"schtasks timed out after {SCHTASKS_TIMEOUT:.0f}s",
        )


def register_health_task(
    exe: str,
    args: str = "",
    *,
    every_minutes: int = 5,
    as_system: bool = False,
    app_dir: str | None = None,
) -> bool:
    """Create (or replace) the health task. False off Windows or on refusal —
    callers treat this as best-effort: a daemon without its healer is still a
    daemon, and the caller logs the outcome.

    Pass `app_dir` whenever the command needs a working directory to resolve
    (a source install's ``-m palctl.daemon``); see task_run_string."""
    if not _on_windows():
        return False
    try:
        return (
            _run(
                create_task_command(
                    exe, args, every_minutes=every_minutes, as_system=as_system,
                    app_dir=app_dir,
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
