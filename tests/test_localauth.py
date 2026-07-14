"""The token gates who can drive the daemon on a shared box, so it has to be
stable (the daemon and GUI must derive the same value) and persisted."""

import stat
import sys

import palctl.localauth as la


def test_token_is_created_persisted_and_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)

    first = la.get_or_create_token()
    assert first and len(first) >= 20
    assert la.token_path().exists()

    # A second call (as the GUI would, in another process) reads the same file.
    assert la.get_or_create_token() == first


def test_token_file_is_owner_only_on_posix(tmp_path, monkeypatch):
    if sys.platform.startswith("win"):
        return  # POSIX perms don't apply; the per-user dir is the boundary
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)
    la.get_or_create_token()
    mode = stat.S_IMODE(la.token_path().stat().st_mode)
    # No group/other bits — created 0o600 from the outset, never world-readable.
    assert mode & 0o077 == 0


def test_token_header_name():
    assert la.TOKEN_HEADER == "X-Palctl-Token"
