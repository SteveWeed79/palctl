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
    try:
        path.write_text(token, encoding="utf-8")
        # Best-effort tighten on POSIX; on Windows the per-user config dir is the
        # boundary that matters and chmod is a no-op.
        try:
            import os

            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # If we can't persist it, the daemon and GUI can't agree on a token and
        # the GUI will get 401s — a safe (closed) failure, not an open one.
        pass
    return token
