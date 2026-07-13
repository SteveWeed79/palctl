"""Profiles are the foundation for multi-server, so the store round-trips a full
Config (including nested watchdog/schedule/discord) and refuses names that would
escape the profiles directory."""

import palctl.profiles as pr
from palctl.config import Config


def test_save_list_load_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "config_dir", lambda: tmp_path)

    cfg = Config()
    cfg.api_port = 9001
    cfg.watchdog.memory_limit_mb = 8000
    cfg.schedule.auto_update = True

    pr.save_profile("home", cfg)
    assert pr.list_profiles() == ["home"]

    loaded = pr.load_profile("home")
    assert loaded.api_port == 9001
    assert loaded.watchdog.memory_limit_mb == 8000
    assert loaded.schedule.auto_update is True

    pr.set_active("home")
    assert pr.active_profile() == "home"

    pr.delete_profile("home")
    assert pr.list_profiles() == []


def test_active_is_none_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "config_dir", lambda: tmp_path)
    assert pr.active_profile() is None


def test_profile_name_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "config_dir", lambda: tmp_path)
    pr.save_profile("../evil", Config())
    # Nothing escaped into or above the config dir.
    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path.parent / "evil.json").exists()
    # It was stored under a sanitized name inside profiles/.
    assert pr.list_profiles() and all(".." not in n and "/" not in n for n in pr.list_profiles())
