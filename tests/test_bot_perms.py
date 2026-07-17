"""Admin-permission decision for the Discord bot.

The bot module imports discord.py; these tests only exercise the pure
`_admin_allowed` helper, which has no Discord dependency of its own.
"""

import pytest

from palctl.bot import _admin_allowed

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
