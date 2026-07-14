"""
Shared secret for the localhost daemon API.

The daemon's control API binds 127.0.0.1 only — but "only on this box" still
means any other local process or user account can drive it: start, stop, restore
a backup, kick, ban. A token gates that. It lives in a file in the per-user
config dir, which the daemon and the GUI (both running as you) read; a random
local program doesn't have it, so it can't touch your server.

This is defence-in-depth, not a fortress — the real boundary is still
"127.0.0.1 only, never port-forwarded". But on a shared PC it's the difference
between "my user" and "anyone logged in".
"""

from __future__ import annotations

import os
import secrets

from .config import config_dir

TOKEN_HEADER = "X-Palctl-Token"


def token_path():
    return config_dir() / "daemon_token"


def get_or_create_token() -> str:
    """Read the shared token, creating it on first use. Same value for the daemon
    and the GUI because they read the same file."""
    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(32)
    # Create with 0o600 from the outset (O_EXCL) rather than write_text + chmod,
    # which leaves a brief window where another local user could read the token.
    # On POSIX the mode is applied at creation (minus umask, which never adds
    # group/other bits here); on Windows mode is ignored but the per-user config
    # dir is the real boundary anyway.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process created it between our read above and now — prefer the
        # persisted value so the daemon and GUI agree.
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        return token
    except OSError:
        # Can't persist it: the daemon and GUI can't agree on a token and the
        # GUI gets 401s — a safe (closed) failure, not an open one.
        return token
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except OSError:
        pass
    return token
