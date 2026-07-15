"""
Best-effort check for a newer palctl release on GitHub.

A distributed desktop tool where nobody knows they're on an old build is a
support nightmare. On startup the daemon quietly asks GitHub for the latest
release tag and, if it's newer, emits an event (Discord + GUI) — it never
downloads or installs anything on its own.
"""

from __future__ import annotations

import json
import urllib.request

from . import __version__

REPO = "SteveWeed79/palctl"


def _parse_version(v: str) -> tuple[int, ...]:
    """Turn 'v1.2.3' / '1.2' into a comparable tuple, tolerating junk suffixes."""
    parts: list[int] = []
    for chunk in v.lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(current: str, latest: str) -> bool:
    a, b = _parse_version(latest), _parse_version(current)
    # Zero-pad to equal length so "1.2.0" isn't treated as newer than "1.2"
    # ((1,2,0) > (1,2) is True as a raw tuple compare).
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def latest_release(repo: str = REPO, timeout: float = 5.0) -> str | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tag = json.load(resp).get("tag_name")
        return tag or None
    except Exception:
        return None


def check(current: str = __version__, repo: str = REPO) -> str | None:
    """Return the latest tag if it's newer than `current`, else None."""
    latest = latest_release(repo)
    return latest if latest and is_newer(current, latest) else None
