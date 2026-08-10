"""
Bundle logs + config into a single zip for a bug report.

Supporting a non-technical host means "send me your logs" has to be one click,
not a scavenger hunt through %APPDATA%. This zips the rotating log files, the
config.json, and a short system summary.

It's safe to share: secrets (admin password, Discord token) live in Windows
Credential Manager and never touch config.json or the logs, so there's nothing
sensitive to redact.
"""

from __future__ import annotations

import contextlib
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from .client import DAEMON_PORT
from .config import CONFIG_PATH, config_dir

# The daemon's service name (see daemon.SERVICE_NAME) — inlined so this module
# stays lightweight and doesn't import the whole daemon just to name a service.
_DAEMON_SERVICE = "palctl-daemon"


def _summary() -> str:
    return "\n".join(
        [
            "palctl diagnostics",
            f"generated: {datetime.now().isoformat(timespec='seconds')}",
            f"python:    {sys.version.split()[0]}",
            f"platform:  {platform.platform()}",
            f"config_dir: {config_dir()}",
        ]
    )


def _service_report() -> str:
    """State of the daemon (and game-server) services, with the logon account
    and any start-failure reason. This is the piece that makes a "daemon won't
    start" report diagnosable without getting onto the box: a service stuck on a
    logon failure (Error 1069) or running under the wrong account shows up here
    instead of looking like an unexplained down daemon."""
    from . import procs

    names = [_DAEMON_SERVICE]
    with contextlib.suppress(Exception):
        from .config import Config

        svc = Config.load().service_name
        if svc and svc not in names:
            names.append(svc)

    blocks: list[str] = []
    for name in names:
        lines = [f"### {name}", f"state: {procs.service_state(name)}"]
        reason = procs.service_failure_reason(name)
        if reason:
            lines.append(f"start failure: {reason}")
        lines.append(procs.service_diagnostics(name))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _decisions_report() -> str:
    """What palctl decided, and why, straight from the running daemon.

    The single most useful page in a bug report and the one a log tail buries:
    an admin sending "it says my server is down and it isn't" is describing a
    decision, not an event. Fetched over the local control API because only the
    running daemon has it; a daemon that isn't running simply contributes a
    note saying so, which is itself worth knowing.
    """
    try:
        import httpx

        from .localauth import TOKEN_HEADER, get_or_create_token

        r = httpx.get(
            f"http://127.0.0.1:{DAEMON_PORT}/decisions?n=200",
            headers={TOKEN_HEADER: get_or_create_token()},
            timeout=5.0,
        )
        r.raise_for_status()
        rows = r.json().get("decisions", [])
    except Exception as e:
        return f"(the daemon did not answer, so no decisions could be read: {e})"
    if not rows:
        return "(the daemon has recorded no decisions yet)"
    out = []
    for d in rows:
        when = datetime.fromtimestamp(d.get("last_at", 0)).isoformat(timespec="seconds")
        times = f" x{d['count']}" if d.get("count", 1) > 1 else ""
        out.append(f"{when}  {d.get('action', '?')}{times}: {d.get('why', '')}")
    return "\n".join(out)


def build_bundle(dest_zip: Path) -> Path:
    """Write a diagnostics zip to `dest_zip` and return it."""
    log_dir = config_dir() / "logs"
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.txt", _summary())
        # Never let a service probe (a slow/absent sc.exe or systemctl) keep the
        # bundle from being written — the logs are the point.
        with contextlib.suppress(Exception):
            z.writestr("services.txt", _service_report())
        with contextlib.suppress(Exception):
            z.writestr("decisions.txt", _decisions_report())
        if CONFIG_PATH.exists():
            z.write(CONFIG_PATH, "config.json")
        if log_dir.is_dir():
            for f in sorted(log_dir.glob("*.log*")):
                z.write(f, f"logs/{f.name}")
    return dest_zip
