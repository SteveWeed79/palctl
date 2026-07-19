"""fetch.open_url is what stands between setup and the bare `_ssl.c:1010`
stop-dead: system trust first, one certifi retry on a verification failure,
and a message that names the situation when both fail. Verification is never
disabled — both-fail still fails closed."""

import ssl
import urllib.error
import urllib.request

import pytest

from palctl import fetch


class _Resp:
    pass


def _verify_error() -> urllib.error.URLError:
    return urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer"))


def test_open_url_uses_system_trust_first(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None, context=None):
        calls.append(context)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert isinstance(fetch.open_url("https://x", timeout=5), _Resp)
    assert calls == [None]  # no custom context — the system trust decided


def test_open_url_retries_with_certifi_on_verification_failure(monkeypatch):
    certifi = pytest.importorskip("certifi")
    contexts = []

    def fake_urlopen(url, timeout=None, context=None):
        contexts.append(context)
        if context is None:
            raise _verify_error()
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert isinstance(fetch.open_url("https://x", timeout=5), _Resp)
    assert contexts[0] is None
    # The retry really used certifi's CA bundle, not a disabled check.
    assert isinstance(contexts[1], ssl.SSLContext)
    assert contexts[1].verify_mode == ssl.CERT_REQUIRED
    assert certifi.where()  # sanity: the bundle exists


def test_open_url_fails_closed_with_a_useful_message(monkeypatch):
    def fake_urlopen(url, timeout=None, context=None):
        raise _verify_error()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OSError, match="antivirus or proxy"):
        fetch.open_url("https://x", timeout=5)


def test_open_url_propagates_non_certificate_errors_unchanged(monkeypatch):
    # A refused connection / DNS failure is not a trust problem — no retry,
    # no rewording; the caller's own error handling sees the original.
    def fake_urlopen(url, timeout=None, context=None):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        fetch.open_url("https://x", timeout=5)
