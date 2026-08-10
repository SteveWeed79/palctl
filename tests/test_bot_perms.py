"""Admin-permission decision for the Discord bot.

The bot module imports discord.py; these tests only exercise the pure
`_admin_allowed` helper, which has no Discord dependency of its own.
"""

import pytest

from palctl.bot import _admin_allowed, command_allowed_here

ROLE = 111111111111111111
USER = 222222222222222222
OTHER = 999999999999999999


class TestAdminAllowed:
    def test_no_id_configured_falls_back_to_manage_guild(self):
        assert _admin_allowed(0, USER, set(), has_manage_guild=True) is True
        assert _admin_allowed(0, USER, set(), has_manage_guild=False) is False

    def test_manage_guild_ignored_once_an_id_is_set(self):
        # With an explicit admin id, Manage Server no longer grants access on
        # its own — you must match the id.
        assert _admin_allowed(ROLE, USER, set(), has_manage_guild=True) is False

    def test_matches_when_caller_holds_the_role(self):
        assert _admin_allowed(ROLE, USER, {ROLE}, has_manage_guild=False) is True

    def test_matches_when_id_is_the_callers_user_id(self):
        # A user ID pasted into the (historically role-only) field still works.
        assert _admin_allowed(USER, USER, set(), has_manage_guild=False) is True

    def test_denies_a_stranger(self):
        assert _admin_allowed(ROLE, OTHER, {OTHER}, has_manage_guild=False) is False

    def test_typo_id_matching_nothing_denies(self):
        assert _admin_allowed(OTHER, USER, {ROLE}, has_manage_guild=False) is False

    @pytest.mark.parametrize("has_manage_guild", [True, False])
    def test_role_among_several_still_matches(self, has_manage_guild):
        assert _admin_allowed(
            ROLE, USER, {OTHER, ROLE, 333}, has_manage_guild=has_manage_guild
        ) is True


# ---------------- guild scoping ----------------
#
# Slash commands are registered globally, so they appear in every guild the bot
# is in — and a Discord application is PUBLIC by default, so anyone holding the
# client ID can invite the bot to a guild of their own. In that guild they have
# Manage Server by definition, which is exactly what admin access falls back to
# when admin_role_id is unset (the default). Without a guild check that is a
# working path to /stop, /restore and /update on someone else's server.


def test_commands_from_the_home_guild_are_allowed():
    assert command_allowed_here(1234, 1234) is True


def test_commands_from_another_guild_are_rejected():
    """The attack: the bot invited to a guild the operator has never seen."""
    assert command_allowed_here(1234, 9999) is False


def test_no_known_home_guild_stays_permissive():
    """Neither guild_id nor a notification channel configured — we genuinely
    don't know which guild is the operator's, and guessing wrong would lock the
    real operator out of their own bot."""
    assert command_allowed_here(0, 9999) is True


def test_dms_are_passed_through_to_the_admin_check():
    """There is no Manage Server in a DM, so _admin_allowed denies unless the
    caller IS the configured admin user — which is a legitimate way to run it."""
    assert command_allowed_here(1234, None) is True
    # ...and the admin check is what actually gates it there:
    assert _admin_allowed(0, caller_id=42, caller_role_ids=set(),
                              has_manage_guild=False) is False


def test_the_escalation_path_is_closed_end_to_end():
    """Attacker owns their guild (Manage Server = True) and the operator left
    admin_role_id at its default. Before guild scoping this was full admin."""
    attacker_has_manage_guild = True
    # The old check on its own still says yes — that is the fallback working as
    # designed, for the operator's own guild.
    assert _admin_allowed(0, caller_id=777, caller_role_ids=set(),
                              has_manage_guild=attacker_has_manage_guild) is True
    # The guild gate in front of it is what makes the difference.
    assert command_allowed_here(home_guild_id=1234, interaction_guild_id=9999) is False


# The pure helper above is only half the control — the other half is that it's
# actually wired in front of every command. discord.py's CommandTree._call does
# `await self.interaction_check(interaction)`, so an instance attribute takes
# precedence over the no-op default. If that wiring ever breaks, the helper
# still passes its tests while the door stands open, so pin the wiring too.


def test_the_guild_gate_is_wired_into_the_command_tree(tmp_path):
    import asyncio
    import tempfile
    from pathlib import Path

    from palctl.bot import PalBot
    from palctl.config import Config
    from palctl.events import EventBus, SessionStore

    cfg = Config()
    cfg.discord.guild_id = 1234
    store = SessionStore(Path(tempfile.mkdtemp()) / "sessions.db")
    bot = PalBot(cfg, api=None, bus=EventBus(), store=store, scheduler=None)

    assert bot.tree.interaction_check.__func__ is PalBot._guild_check
    assert bot._home_guild_id() == 1234

    class _Interaction:
        def __init__(self, guild_id):
            self.guild_id = guild_id

        command = type("C", (), {"name": "stop"})()
        user = type("U", (), {"id": 777})()

        class response:
            @staticmethod
            async def send_message(*a, **k):
                pass

    assert asyncio.run(bot.tree.interaction_check(_Interaction(1234))) is True
    assert asyncio.run(bot.tree.interaction_check(_Interaction(9999))) is False
    store.close()


def test_home_guild_falls_back_to_the_notification_channels_guild(tmp_path):
    import tempfile
    from pathlib import Path

    from palctl.bot import PalBot
    from palctl.config import Config
    from palctl.events import EventBus, SessionStore

    cfg = Config()
    cfg.discord.channel_id = 555  # no explicit guild_id
    store = SessionStore(Path(tempfile.mkdtemp()) / "sessions.db")
    bot = PalBot(cfg, api=None, bus=EventBus(), store=store, scheduler=None)

    # Stand in for the resolved channel: the guild that owns it is the home one.
    bot.get_channel = lambda cid: (
        type("Ch", (), {"guild": type("G", (), {"id": 4242})()})()
        if cid == 555 else None
    )
    assert bot._home_guild_id() == 4242
    store.close()
