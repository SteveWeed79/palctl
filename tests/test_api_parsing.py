"""What the REST client does with a server that answers *badly*.

The reachable-or-not paths are covered elsewhere. This file pins the middle
case, which is the dangerous one: the Palworld server is answering, so nothing
looks down, but the payload can't be read. Every caller of this client catches
`PalApiError` and nothing else — the daemon's poll handler included — so a
parse failure that escapes as ValueError skips `_maybe_autorecover()` entirely
and a sick server is never recovered while the log fills with "Poll failed".
"""

from __future__ import annotations

import asyncio
import types
from typing import ClassVar

import pytest

pytest.importorskip("httpx")

from palctl.api import Metrics, PalApi, PalApiError

_MALFORMED = object()  # stand-in for "the body isn't JSON at all"


class _Resp:
    def __init__(self, status, content, json_value, text):
        self.status_code = status
        self.content = content
        self.text = text
        self._json = json_value

    def json(self):
        if self._json is _MALFORMED:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


class _Transport:
    def __init__(self, resp):
        self._resp = resp
        self.is_closed = False

    async def request(self, *_a, **_k):
        return self._resp


def _client(status=200, content=b"{}", json_value=None, text=""):
    """A PalApi whose transport is replaced by one canned response."""
    api = PalApi(password="x")
    api._client = _Transport(_Resp(status, content, json_value, text))
    return api


# ---------------- the payload can't be parsed ----------------


def test_a_garbled_metrics_field_raises_palapierror_not_valueerror():
    """`serverfps: "n/a"` is the shape this is really about: HTTP 200, valid
    JSON, one field the server filled in wrong."""
    api = _client(json_value={"serverfps": "n/a", "currentplayernum": 3})
    with pytest.raises(PalApiError):
        asyncio.run(api.metrics())


def test_a_null_where_a_number_belongs_raises_palapierror():
    # int(None) is TypeError, not ValueError — a separate escape route.
    api = _client(json_value={"serverfps": None})
    with pytest.raises(PalApiError):
        asyncio.run(api.metrics())


def test_a_non_json_body_is_a_failure_not_an_empty_server():
    """A proxy error page or captive portal served with HTTP 200. This used to
    become {"raw": text}, which from_json read as an object full of defaults —
    so it reported a healthy server with 0 players instead of failing."""
    api = _client(content=b"<html>Gateway Timeout</html>", json_value=_MALFORMED, text="<html>...")
    with pytest.raises(PalApiError):
        asyncio.run(api.metrics())


def test_a_json_array_where_an_object_belongs_raises_palapierror():
    api = _client(content=b"[]", json_value=[])
    with pytest.raises(PalApiError):
        asyncio.run(api.metrics())


def test_a_malformed_player_entry_raises_palapierror():
    api = _client(json_value={"players": [{"ping": "not-a-number"}]})
    with pytest.raises(PalApiError):
        asyncio.run(api.players())


def test_players_that_are_not_objects_raise_palapierror():
    api = _client(json_value={"players": ["steve"]})
    with pytest.raises(PalApiError):
        asyncio.run(api.players())


def test_an_empty_body_still_parses_to_defaults():
    """A 200 with no body at all is the documented shape for the write
    endpoints, and must not start failing."""
    api = _client(content=b"", json_value=None)
    assert asyncio.run(api.metrics()) == Metrics(0, 0, 0.0, 0, 0, 0, 0)


def test_a_good_payload_still_parses():
    api = _client(
        json_value={
            "serverfps": 60,
            "currentplayernum": 4,
            "serverframetime": 16.6,
            "maxplayernum": 32,
            "uptime": 120,
            "basecampnum": 7,
            "days": 3,
        }
    )
    m = asyncio.run(api.metrics())
    assert (m.server_fps, m.current_players, m.max_players) == (60, 4, 32)


# ---------------- and the reason it matters ----------------


def test_is_alive_reports_false_for_a_server_answering_garbage():
    """`is_alive` catches PalApiError only. A server answering nonsense used to
    crash it instead of returning False, so every caller that asks 'is it up?'
    got an exception rather than an answer."""
    api = _client(json_value={"serverfps": "n/a"})
    assert asyncio.run(api.is_alive()) is False


def test_a_garbled_metrics_reply_still_reaches_auto_recovery():
    """The point of the whole file, driven through the real `Daemon._poll`.

    The handler there is `except PalApiError`. A ValueError from the parser
    didn't match it, so it escaped to the outer loop — which logs "Poll failed"
    and sleeps. `_maybe_autorecover()` never ran, and a server that had started
    answering nonsense was never restarted.
    """
    pytest.importorskip("aiohttp")
    pytest.importorskip("discord")
    import palctl.daemon as daemon_mod

    recovered = []

    class _Stub:
        api = _client(json_value={"serverfps": "n/a"})
        # server_root: the OS sample taken during REST trouble is scoped to the
        # configured install, like every other process lookup.
        cfg = types.SimpleNamespace(
            watchdog=types.SimpleNamespace(crash_confirm_polls=3), server_root=""
        )
        _api_fail_streak = 0
        _alive = True
        _last_metrics = object()
        _history: ClassVar[list] = []

        def _record_os_only_sample(self, _stats):
            pass

        class bus:
            @staticmethod
            async def emit(_e):
                pass

        class tracker:
            @staticmethod
            async def handle_server_down():
                pass

        async def _maybe_autorecover(self):
            recovered.append(True)

    asyncio.run(daemon_mod.Daemon._poll(_Stub()))
    assert recovered == [True], "a malformed payload must still reach auto-recovery"
