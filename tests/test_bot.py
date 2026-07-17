"""Discord bot logic, driven with plain stub objects (no live Discord).

test_bot_perms.py already pins the pure `_admin_allowed` decision. This file
covers the surrounding pieces where a regression is silent and dangerous:
the `_is_admin` gate wiring, `_moderate` name/ID resolution, the bounded event
pump's drop-oldest behaviour, and run_bot's connect/retry lifecycle. Every test
calls the real methods with a stub `self` / stub client, so nothing here needs a
Discord connection."""

import asyncio
import types

import pytest

pytest.importorskip("discord")

import discord  # noqa: E402

import palctl.bot as bot_mod  # noqa: E402
from palctl.bot import PalBot  # noqa: E402
from palctl.config import Config  # noqa: E402
from palctl.events import Event  # noqa: E402

ROLE = 111111111111111111
USER = 222222222222222222
OTHER = 999999999999999999


# ---------------- _is_admin (interaction -> _admin_allowed wiring) ----------------


def _interaction(*, user_id=USER, role_ids=(), manage_guild=False, has_perms=True):
    perms = types.SimpleNamespace(manage_guild=manage_guild) if has_perms else None
    user = types.SimpleNamespace(
        id=user_id,
        roles=[types.SimpleNamespace(id=r) for r in role_ids],
        guild_permissions=perms,
    )
    return types.SimpleNamespace(user=user)


def _bot_with_admin_id(admin_id):
    cfg = Config()
    cfg.discord.admin_role_id = admin_id
    return types.SimpleNamespace(_cfg=cfg)


def test_is_admin_falls_back_to_manage_guild_when_unconfigured():
    stub = _bot_with_admin_id(0)
    assert PalBot._is_admin(stub, _interaction(manage_guild=True)) is True
    assert PalBot._is_admin(stub, _interaction(manage_guild=False)) is False


def test_is_admin_matches_a_configured_role():
    stub = _bot_with_admin_id(ROLE)
    assert PalBot._is_admin(stub, _interaction(role_ids=(OTHER, ROLE))) is True


def test_is_admin_matches_a_configured_user_id():
    stub = _bot_with_admin_id(USER)
    assert PalBot._is_admin(stub, _interaction(user_id=USER, role_ids=())) is True


def test_is_admin_ignores_manage_guild_once_an_id_is_set():
    stub = _bot_with_admin_id(ROLE)
    # Manage Server is true, but the id doesn't match -> denied.
    assert PalBot._is_admin(stub, _interaction(user_id=OTHER, manage_guild=True)) is False


def test_is_admin_survives_a_user_without_guild_context():
    # In a DM, guild_permissions/roles are absent; getattr fallbacks must not
    # raise, and with no admin id configured that means "no Manage Server".
    stub = _bot_with_admin_id(0)
    bare = types.SimpleNamespace(user=types.SimpleNamespace(id=USER))
    assert PalBot._is_admin(stub, bare) is False


# ---------------- _moderate (name / user-id resolution) ----------------


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def defer(self):
        self.messages.append(("defer", None))

    async def send_message(self, content=None, *, ephemeral=False, **kw):
        self.messages.append(("send_message", content))


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kw):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class _FakeApi:
    def __init__(self, players):
        self._players = players
        self.kicked = []
        self.banned = []

    async def players(self):
        return list(self._players)

    async def kick(self, user_id, reason):
        self.kicked.append((user_id, reason))

    async def ban(self, user_id, reason):
        self.banned.append((user_id, reason))


class _StubBot:
    def __init__(self, api, *, admin=True):
        self._api = api
        self._admin = admin

    def _is_admin(self, interaction):
        return self._admin


def _player(name, user_id):
    return types.SimpleNamespace(name=name, user_id=user_id)


def _moderate(bot, interaction, name, reason="r", action="kick"):
    asyncio.run(PalBot._moderate(bot, interaction, name, reason, action))


def test_moderate_denies_non_admins_without_touching_the_api():
    api = _FakeApi([_player("Steve", "u1")])
    bot = _StubBot(api, admin=False)
    it = _FakeInteraction()
    _moderate(bot, it, "Steve")
    assert api.kicked == [] and api.banned == []
    assert any("send_message" == m[0] for m in it.response.messages)


def test_moderate_kicks_a_single_named_player():
    api = _FakeApi([_player("Steve", "u1"), _player("Bob", "u2")])
    it = _FakeInteraction()
    _moderate(_StubBot(api), it, "steve", action="kick")  # case-insensitive
    assert api.kicked == [("u1", "r")]


def test_moderate_bans_via_the_ban_action():
    api = _FakeApi([_player("Steve", "u1")])
    _moderate(_StubBot(api), _FakeInteraction(), "Steve", reason="grief", action="ban")
    assert api.banned == [("u1", "grief")]
    assert api.kicked == []


def test_moderate_exact_user_id_wins_over_a_name_collision():
    # Two players named "Steve"; passing the exact user ID must hit that one and
    # never trigger the duplicate-name refusal.
    api = _FakeApi([_player("Steve", "u1"), _player("Steve", "u2")])
    _moderate(_StubBot(api), _FakeInteraction(), "u2", action="kick")
    assert api.kicked == [("u2", "r")]


def test_moderate_refuses_an_ambiguous_name():
    api = _FakeApi([_player("Steve", "u1"), _player("Steve", "u2")])
    it = _FakeInteraction()
    _moderate(_StubBot(api), it, "Steve", action="kick")
    assert api.kicked == []  # refused, not a coin-flip
    msg = it.followup.messages[-1]
    assert "u1" in msg and "u2" in msg  # both candidates surfaced


def test_moderate_reports_a_player_who_isnt_online():
    api = _FakeApi([_player("Steve", "u1")])
    it = _FakeInteraction()
    _moderate(_StubBot(api), it, "Ghost", action="kick")
    assert api.kicked == []
    assert "isn't online" in it.followup.messages[-1]


# ---------------- _on_event (bounded queue, drop-oldest) ----------------


def test_on_event_queues_without_blocking():
    async def go():
        stub = types.SimpleNamespace(_events=asyncio.Queue(maxsize=10))
        await PalBot._on_event(stub, Event("join", "a"))
        await PalBot._on_event(stub, Event("leave", "b"))
        return stub._events

    q = asyncio.run(go())
    assert q.qsize() == 2


def test_on_event_drops_oldest_when_full():
    async def go():
        stub = types.SimpleNamespace(_events=asyncio.Queue(maxsize=2))
        await PalBot._on_event(stub, Event("join", "e1"))
        await PalBot._on_event(stub, Event("join", "e2"))  # queue now full
        await PalBot._on_event(stub, Event("server_down", "e3"))  # evicts e1
        drained = []
        while not stub._events.empty():
            drained.append(stub._events.get_nowait().message)
        return drained

    # Oldest (e1) dropped; newest kept — a "down" alert must not be lost to
    # stale join/leave noise during a Discord outage.
    assert asyncio.run(go()) == ["e2", "e3"]


# ---------------- run_bot (connect / retry lifecycle) ----------------


class _FakeBus:
    def __init__(self):
        self.events = []
        self.on_any_calls = 0
        self.off_any_calls = 0

    def on_any(self, handler):
        self.on_any_calls += 1

    def off_any(self, handler):
        self.off_any_calls += 1

    async def emit(self, event):
        self.events.append(event)


def _make_fake_bot(behaviours, created):
    """A PalBot stand-in whose start() consumes one behaviour per attempt: an
    exception instance to raise, or None to return cleanly. `connect_first` on a
    behaviour tuple flips _status_started before raising (a post-connect drop)."""

    class _FakeBot:
        def __init__(self, cfg, api, bus, store, sched):
            self._bus = bus
            self._on_event = object()  # sentinel handler for off_any
            self._status_started = False
            self._closed = False
            created.append(self)

        def is_closed(self):
            return self._closed

        async def close(self):
            self._closed = True

        async def start(self, token):
            action = behaviours.pop(0)
            if action == "connect_then_fail":
                self._status_started = True
                raise ConnectionError("dropped after connect")
            if action is not None:
                raise action
            return  # clean shutdown

    return _FakeBot


def _run_bot(monkeypatch, behaviours, *, token="tok", enabled=True):
    created = []
    monkeypatch.setattr(bot_mod, "PalBot", _make_fake_bot(behaviours, created))
    monkeypatch.setattr(bot_mod, "get_discord_token", lambda: token)
    sleeps = []

    async def _no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(bot_mod.asyncio, "sleep", _no_sleep)
    bus = _FakeBus()
    cfg = Config()
    cfg.discord.enabled = enabled
    asyncio.run(bot_mod.run_bot(cfg, object(), bus, object(), object()))
    return created, bus, sleeps


def test_run_bot_is_a_noop_when_disabled_or_tokenless(monkeypatch):
    created, bus, _ = _run_bot(monkeypatch, [], token="", enabled=True)
    assert created == [] and bus.events == [] and bus.off_any_calls == 0


def test_run_bot_gives_up_on_a_rejected_token(monkeypatch):
    created, bus, sleeps = _run_bot(monkeypatch, [discord.LoginFailure("bad")])
    assert len(created) == 1  # a bad token never retries
    assert sleeps == []
    assert any("rejected" in e.message for e in bus.events)
    assert bus.off_any_calls == 1  # the dead handler was unhooked


def test_run_bot_retries_the_initial_connect_then_succeeds(monkeypatch):
    created, bus, sleeps = _run_bot(
        monkeypatch, [ConnectionError("net not up"), None]
    )
    assert len(created) == 2  # rebuilt and retried
    assert sleeps[0] == 15  # first backoff: 15 * 2**0
    assert bus.off_any_calls == 2  # each attempt unhooked its handler
    assert any("retrying" in e.message for e in bus.events)


def test_run_bot_gives_up_after_a_connected_session_ends(monkeypatch):
    created, bus, sleeps = _run_bot(monkeypatch, ["connect_then_fail"])
    assert len(created) == 1  # a real session that dropped is discord.py's job
    assert sleeps == []
    assert any("stopped" in e.message for e in bus.events)


# ---------------- pure helpers (autocomplete / health / leaderboard / schedule) ----

from datetime import datetime  # noqa: E402

from palctl.api import PalApiError  # noqa: E402
from palctl.bot import (  # noqa: E402
    _ConfirmView,
    _format_leaderboard,
    _format_schedule,
    _health_fields,
    _match_choices,
)


def test_match_choices_prefix_before_contains():
    opts = ["Steve", "SteveW", "Steven", "bob", "misteve"]
    # prefix matches first (in input order), then substring matches
    assert _match_choices(opts, "ste") == ["Steve", "SteveW", "Steven", "misteve"]


def test_match_choices_empty_current_returns_all_capped():
    assert _match_choices(["a", "b", "c"], "", limit=2) == ["a", "b"]


def test_match_choices_case_insensitive_and_capped():
    opts = [f"p{i}" for i in range(30)]
    assert _match_choices(opts, "P", limit=25) == opts[:25]


def test_health_fields_warns_near_the_memory_limit():
    fields, warn = _health_fields(
        memory_mb=11500, limit_mb=12000, cpu=10, fps=60, frame_time=16.0, ttl_minutes=None
    )
    assert warn is True
    assert any("96%" in v for _, v in fields)


def test_health_fields_warns_on_a_short_leak_forecast():
    _, warn = _health_fields(
        memory_mb=5000, limit_mb=12000, cpu=10, fps=60, frame_time=16.0, ttl_minutes=45
    )
    assert warn is True


def test_health_fields_calm_when_low_and_stable():
    fields, warn = _health_fields(
        memory_mb=5000, limit_mb=12000, cpu=10, fps=60, frame_time=16.0, ttl_minutes=None
    )
    assert warn is False
    assert any("no restart forecast" in v for _, v in fields)


def test_format_leaderboard_medals_then_numbers():
    out = _format_leaderboard([("A", 120.0), ("B", 60.0), ("C", 30.0), ("D", 6.0)])
    lines = out.splitlines()
    assert lines[0].startswith("🥇") and "2.0h" in lines[0]
    assert "4." in lines[3] and "0.1h" in lines[3]


def test_format_schedule_off_when_disabled():
    cfg = Config()
    cfg.schedule.enabled = False
    assert "off" in _format_schedule(cfg.schedule, datetime(2026, 7, 17, 12, 0)).lower()


def test_format_schedule_lists_restart_and_backups():
    out = _format_schedule(Config().schedule, datetime(2026, 7, 17, 12, 0))
    assert "Daily restart" in out and "Backups every" in out


# ---------------- _server_power (start / stop delegation + gating) ----------------


class _StubSched:
    def __init__(self, result):
        self.result = result
        self.started = False
        self.stopped = False

    async def start_server(self):
        self.started = True
        return self.result

    async def stop_server(self):
        self.stopped = True
        return self.result


class _PowerBot:
    def __init__(self, sched, *, admin=True):
        self._sched = sched
        self._admin = admin

    def _is_admin(self, interaction):
        return self._admin


def test_server_power_start_maps_ok_to_starting_message():
    sched = _StubSched("ok")
    it = _FakeInteraction()
    asyncio.run(PalBot._server_power(_PowerBot(sched), it, "start"))
    assert sched.started
    assert "Starting" in it.followup.messages[-1]


def test_server_power_start_maps_busy():
    sched = _StubSched("busy")
    it = _FakeInteraction()
    asyncio.run(PalBot._server_power(_PowerBot(sched), it, "start"))
    assert "mid-operation" in it.followup.messages[-1].lower()


def test_server_power_denies_non_admin_without_calling_scheduler():
    sched = _StubSched("ok")
    it = _FakeInteraction()
    asyncio.run(PalBot._server_power(_PowerBot(sched, admin=False), it, "start"))
    assert not sched.started
    assert any(m[0] == "send_message" for m in it.response.messages)


# ---------------- _ConfirmView (only the author may press) ----------------


def _it_with_user(uid):
    it = _FakeInteraction()
    it.user = types.SimpleNamespace(id=uid)
    return it


def test_confirm_view_only_the_author_can_press():
    async def go():
        view = _ConfirmView(author_id=100)
        assert await view.interaction_check(_it_with_user(100)) is True
        stranger = _it_with_user(999)
        assert await view.interaction_check(stranger) is False
        return stranger

    stranger = asyncio.run(go())
    assert any(
        m[0] == "send_message" and "isn't yours" in (m[1] or "")
        for m in stranger.response.messages
    )


# ---------------- autocomplete choice sources ----------------


class _PlayersApi:
    def __init__(self, names):
        self._names = names

    async def players(self):
        return [types.SimpleNamespace(name=n) for n in self._names]


def test_player_name_choices_filters_and_wraps():
    bot = types.SimpleNamespace(_api=_PlayersApi(["Steve", "Bob", "Steven"]))
    choices = asyncio.run(PalBot._player_name_choices(bot, None, "ste"))
    assert [c.value for c in choices] == ["Steve", "Steven"]


def test_player_name_choices_empty_when_api_down():
    class _BadApi:
        async def players(self):
            raise PalApiError("unreachable")

    bot = types.SimpleNamespace(_api=_BadApi())
    assert asyncio.run(PalBot._player_name_choices(bot, None, "x")) == []
