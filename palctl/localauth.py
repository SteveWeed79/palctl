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

import contextlib
import os
import secrets

from .config import config_dir

TOKEN_HEADER = "X-Palctl-Token"


def token_path():
    return config_dir() / "daemon_token"


def _read_token(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_or_create_token() -> str:
    """Read the shared token, creating it on first use. Same value for the daemon
    and the GUI because they read the same file.

    An *empty* token file is treated as no token at all and replaced. It cannot
    be honoured (an empty secret would authenticate everyone) and it cannot be
    ignored either: a zero-byte file used to make every caller mint a fresh
    random token that was never persisted, so the daemon and the GUI could never
    agree again — permanent 401s that no restart fixes, reported to the user as
    a config-dir mismatch, which is not the problem at all. A crash or a full
    disk between creating the file and writing it is enough to get there, and
    the write failure below is swallowed by design, so this is reachable without
    anything exotic happening.
    """
    path = token_path()
    # Bounded: each pass either returns a token or removes exactly one unusable
    # file. Two processes racing to replace the same empty file both make
    # progress, and neither spins.
    for _attempt in range(3):
        existing = _read_token(path)
        if existing:
            return existing

        token = secrets.token_urlsafe(32)
        # Create with 0o600 from the outset (O_EXCL) rather than write_text +
        # chmod, which leaves a brief window where another local user could read
        # the token. On POSIX the mode is applied at creation (minus umask,
        # which never adds group/other bits here); on Windows mode is ignored
        # but the per-user config dir is the real boundary anyway.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # It exists but read as empty — either another process is between
            # its create and its write (retry reads the value it lands), or it
            # is a leftover empty file (unlink it and create it properly).
            try:
                os.unlink(path)
            except OSError:
                return token  # can't clear it; fail closed rather than loop
            continue
        except OSError:
            # Can't persist it at all: the daemon and GUI can't agree and the
            # GUI gets 401s — a safe (closed) failure, not an open one.
            return token

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
        except OSError:
            # Don't leave a zero-byte file behind to poison every later call —
            # that is exactly the permanent-401 trap described above.
            with contextlib.suppress(OSError):
                os.unlink(path)
        return token

    return _read_token(path) or secrets.token_urlsafe(32)
