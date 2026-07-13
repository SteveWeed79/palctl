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

import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from .config import CONFIG_PATH, config_dir


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


def build_bundle(dest_zip: Path) -> Path:
    """Write a diagnostics zip to `dest_zip` and return it."""
    log_dir = config_dir() / "logs"
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.txt", _summary())
        if CONFIG_PATH.exists():
            z.write(CONFIG_PATH, "config.json")
        if log_dir.is_dir():
            for f in sorted(log_dir.glob("*.log*")):
                z.write(f, f"logs/{f.name}")
    return dest_zip
