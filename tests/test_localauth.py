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


def test_an_empty_token_file_is_replaced_not_honoured(tmp_path, monkeypatch):
    """A zero-byte token file used to poison auth permanently: every caller
    minted a fresh token that was never persisted, so the daemon and the GUI
    could never agree again — 401s no restart could fix, and the GUI blamed a
    config-dir mismatch instead. A crash or a full disk between creating the
    file and writing it is enough to land there."""
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)
    la.token_path().write_text("", encoding="utf-8")

    daemon_token = la.get_or_create_token()
    gui_token = la.get_or_create_token()  # a different process would do this

    assert daemon_token, "must mint a real token"
    assert daemon_token == gui_token, "daemon and GUI must agree"
    assert la.token_path().read_text(encoding="utf-8").strip() == daemon_token


def test_a_whitespace_only_token_file_is_replaced_too(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)
    la.token_path().write_text("   \n", encoding="utf-8")
    assert la.get_or_create_token() == la.get_or_create_token()


def test_a_failed_write_leaves_no_poisoned_file(tmp_path, monkeypatch):
    """The write failure is swallowed on purpose (it must never crash startup),
    so it has to clean up after itself — leaving the empty file behind is what
    made the failure permanent instead of transient."""
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)

    real_fdopen = la.os.fdopen

    def boom(fd, *a, **k):
        real_fdopen(fd, *a, **k).close()  # consume the fd, then fail
        raise OSError("disk full")

    monkeypatch.setattr(la.os, "fdopen", boom)
    la.get_or_create_token()
    monkeypatch.undo()
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)

    # The next call must be able to create a real, persisted token.
    token = la.get_or_create_token()
    assert token and la.token_path().read_text(encoding="utf-8").strip() == token


def test_an_existing_good_token_is_never_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "config_dir", lambda: tmp_path)
    la.token_path().write_text("a-real-token-value", encoding="utf-8")
    assert la.get_or_create_token() == "a-real-token-value"
