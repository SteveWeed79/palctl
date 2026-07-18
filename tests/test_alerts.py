"""The webhook alerter is the second notification channel (besides Discord + the
GUI/log). It must fire for operational events, stay silent for join/leave
chatter, honour the enable/URL switch, and never let a bad endpoint bubble up."""

import asyncio

import pytest

pytest.importorskip("httpx")

from palctl import alerts as alerts_mod  # noqa: E402
from palctl.alerts import WebhookAlerter  # noqa: E402
from palctl.config import Config  # noqa: E402
from palctl.events import Event, EventBus  # noqa: E402


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _fake_client_factory(sink, *, status=200, raise_exc=None):
    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if raise_exc is not None:
                raise raise_exc
            sink.append((url, json))
            return _FakeResp(status)

    return _FakeClient


def _cfg(enabled=True, url="https://example.test/hook"):
    c = Config()
    c.alert_webhook_enabled = enabled
    c.alert_webhook_url = url
    return c


def _run(coro):
    return asyncio.run(coro)


def test_posts_operational_events_and_skips_chatter(monkeypatch):
    posted: list = []
    monkeypatch.setattr(alerts_mod.httpx, "AsyncClient", _fake_client_factory(posted))
    bus = EventBus()
    WebhookAlerter(_cfg(), bus)

    async def scenario():
        await bus.emit(Event("server_down", "down"))
        await bus.emit(Event("join", "someone joined"))   # chatter, must be skipped
        await bus.emit(Event("watchdog", "restarting"))
        await bus.emit(Event("levelup", "dinged"))         # chatter, must be skipped

    _run(scenario())
    kinds = [body["kind"] for _, body in posted]
    assert kinds == ["server_down", "watchdog"]
    # The message rides under content/text/message so Discord/Slack/ntfy all take it.
    url, body = posted[0]
    assert url == "https://example.test/hook"
    assert body["content"] == body["text"] == body["message"] == "down"


def test_disabled_or_no_url_sends_nothing(monkeypatch):
    posted: list = []
    monkeypatch.setattr(alerts_mod.httpx, "AsyncClient", _fake_client_factory(posted))
    bus = EventBus()
    WebhookAlerter(_cfg(enabled=False), bus)
    WebhookAlerter(_cfg(url=""), bus)  # enabled but no target
    _run(bus.emit(Event("error", "boom")))
    assert posted == []


def test_reconfigure_toggles_delivery(monkeypatch):
    posted: list = []
    monkeypatch.setattr(alerts_mod.httpx, "AsyncClient", _fake_client_factory(posted))
    bus = EventBus()
    alerter = WebhookAlerter(_cfg(enabled=False), bus)
    _run(bus.emit(Event("error", "one")))
    assert posted == []
    alerter.reconfigure(_cfg(enabled=True))  # turned on, same subscription
    _run(bus.emit(Event("error", "two")))
    assert [b["message"] for _, b in posted] == ["two"]


def test_a_broken_webhook_never_raises(monkeypatch):
    monkeypatch.setattr(
        alerts_mod.httpx,
        "AsyncClient",
        _fake_client_factory([], raise_exc=RuntimeError("connection refused")),
    )
    bus = EventBus()
    WebhookAlerter(_cfg(), bus)
    # The bus swallows handler errors too, but the alerter must not even raise.
    _run(bus.emit(Event("server_down", "down")))  # must not raise
