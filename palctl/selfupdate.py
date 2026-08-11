"""
Best-effort check for a newer palctl release on GitHub.

A distributed desktop tool where nobody knows they're on an old build is a
support nightmare. On startup the daemon quietly asks GitHub for the latest
release tag and, if it's newer, emits an event (Discord + GUI) — it never
downloads or installs anything on its own.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from . import __version__, fetch

REPO = "SteveWeed79/palctl"

log = logging.getLogger(__name__)


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


# GitHub's release payload is a few KB; release notes make it variable, so the
# cap is generous. It exists because `json.load(resp)` reads until EOF, and
# "how much will the other end send" is not a question this module gets to
# assume the answer to — a hung or hostile endpoint could otherwise stream
# until the daemon runs out of memory.
_MAX_BYTES = 1 << 20


def latest_release(repo: str = REPO, timeout: float = 5.0) -> str | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        # Through fetch.open_url, not urlopen: that module exists because
        # certificate verification fails outright on a lot of real Windows
        # boxes (AV doing HTTPS interception, a stripped cert store), and it
        # retries against certifi's bundle. Calling urlopen directly meant the
        # update check got none of that — so it silently never worked on
        # precisely the machines fetch.py was written for, which are also the
        # machines most likely to be running an old build.
        with fetch.open_url(req, timeout=timeout) as resp:
            body = resp.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            log.debug("update check: release payload over %d bytes, ignoring", _MAX_BYTES)
            return None
        tag = json.loads(body).get("tag_name")
        return tag or None
    except Exception as e:
        # Best-effort by contract: a failed update check must never take the
        # daemon down or block startup. It shouldn't be *invisible* either —
        # this used to swallow the reason entirely, which is why the fallback
        # above was missing for so long without anyone noticing.
        log.debug("update check failed: %s: %s", e.__class__.__name__, e)
        return None


def check(current: str = __version__, repo: str = REPO) -> str | None:
    """Return the latest tag if it's newer than `current`, else None."""
    latest = latest_release(repo)
    return latest if latest and is_newer(current, latest) else None
