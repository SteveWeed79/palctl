"""Version comparison decides whether we nag the user about an update, so the
ordering and the tolerance for 'v' prefixes / suffixes are pinned. The fetch is
best-effort, but *which client it uses* is pinned too — it has to go through
fetch.open_url, or it stops working on the boxes that module exists for."""

import json
import urllib.request

import pytest

from palctl import fetch, selfupdate


def test_is_newer_ordering():
    assert selfupdate.is_newer("0.1.0", "0.2.0")
    assert selfupdate.is_newer("0.1.0", "v0.1.1")
    assert selfupdate.is_newer("1.9.9", "1.10.0")  # numeric, not lexical
    assert not selfupdate.is_newer("1.0.0", "1.0.0")
    assert not selfupdate.is_newer("2.0.0", "1.9.9")


def test_is_newer_ignores_trailing_zeros():
    # "1.2" and "1.2.0" are the same version — differing tuple lengths must not
    # make one look newer than the other.
    assert not selfupdate.is_newer("1.2", "1.2.0")
    assert not selfupdate.is_newer("1.2.0", "1.2")
    assert selfupdate.is_newer("1.2", "1.2.1")
    assert not selfupdate.is_newer("1.2.1", "1.2")


def test_parse_tolerates_prefix_and_suffix():
    assert selfupdate._parse_version("v1.2.3") == (1, 2, 3)
    assert selfupdate._parse_version("1.2.3-rc1") == (1, 2, 3)
    assert selfupdate._parse_version("") == (0,)


# ---------------- the fetch itself ----------------
#
# It used to call urlopen directly, which meant it bypassed fetch.open_url —
# the module that exists because certificate verification fails outright on a
# lot of real Windows boxes (antivirus doing HTTPS interception, a stripped
# cert store). So the update check silently never worked on precisely the
# machines most likely to be running an old build, and the bare `except`
# ensured nobody ever found out.

class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, n=-1):
        return self._payload if n is None or n < 0 else self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_latest_release_goes_through_the_certifi_fallback(monkeypatch):
    seen = {}

    def fake_open_url(req, timeout):
        seen["req"] = req
        seen["timeout"] = timeout
        return _Resp(json.dumps({"tag_name": "v9.9.9"}).encode())

    monkeypatch.setattr(fetch, "open_url", fake_open_url)
    # Fail loudly if anything reaches urlopen directly again.
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("update check bypassed fetch.open_url"),
    )

    assert selfupdate.latest_release("owner/repo") == "v9.9.9"
    assert isinstance(seen["req"], urllib.request.Request)
    assert seen["req"].full_url.endswith("/repos/owner/repo/releases/latest")
    # The Accept header is the reason a Request is used rather than a bare URL.
    assert seen["req"].get_header("Accept") == "application/vnd.github+json"


def test_an_oversized_payload_is_ignored_rather_than_parsed(monkeypatch):
    """json.load reads until EOF. How much the other end sends is not this
    module's assumption to make."""
    # Valid JSON, deliberately: an oversized body that happens to be malformed
    # would have been rejected anyway, and would prove nothing about the cap.
    huge = json.dumps({"tag_name": "v9.9.9", "body": "y" * (selfupdate._MAX_BYTES + 10)}).encode()
    assert len(huge) > selfupdate._MAX_BYTES
    monkeypatch.setattr(fetch, "open_url", lambda req, timeout: _Resp(huge))
    assert selfupdate.latest_release("owner/repo") is None


def test_a_payload_at_the_limit_still_parses(monkeypatch):
    payload = json.dumps({"tag_name": "v1.0.0", "pad": "y" * 500}).encode()
    assert len(payload) < selfupdate._MAX_BYTES
    monkeypatch.setattr(fetch, "open_url", lambda req, timeout: _Resp(payload))
    assert selfupdate.latest_release("owner/repo") == "v1.0.0"


def test_a_failed_check_stays_best_effort(monkeypatch, caplog):
    """It must never raise into daemon startup — but it should leave a trail,
    which is what the old bare `except: return None` didn't."""

    def boom(req, timeout):
        raise OSError("could not verify the HTTPS connection")

    monkeypatch.setattr(fetch, "open_url", boom)
    with caplog.at_level("DEBUG", logger="palctl.selfupdate"):
        assert selfupdate.latest_release("owner/repo") is None
    assert any("update check failed" in r.getMessage() for r in caplog.records)


def test_a_release_with_no_tag_reads_as_no_answer(monkeypatch):
    monkeypatch.setattr(fetch, "open_url", lambda req, timeout: _Resp(b'{"tag_name": ""}'))
    assert selfupdate.latest_release("owner/repo") is None
