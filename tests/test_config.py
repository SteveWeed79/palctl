import json
from pathlib import Path

import pytest

import palctl.config as config_mod
from palctl.config import Config


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    return path


def test_defaults_when_missing(cfg_path: Path):
    cfg = Config.load()
    assert cfg.api_port == 8212
    assert cfg.watchdog.enabled


def test_save_load_round_trip(cfg_path: Path):
    cfg = Config()
    cfg.api_port = 9000
    cfg.watchdog.memory_limit_mb = 10_000
    cfg.discord.channel_id = 42
    cfg.save()

    loaded = Config.load()
    assert loaded.api_port == 9000
    assert loaded.watchdog.memory_limit_mb == 10_000
    assert loaded.discord.channel_id == 42


def test_unknown_keys_from_other_versions_ignored(cfg_path: Path):
    cfg_path.write_text(
        json.dumps(
            {
                "api_port": 9001,
                "some_future_setting": True,
                "watchdog": {"memory_limit_mb": 9000, "future_knob": 1},
            }
        ),
        encoding="utf-8",
    )
    loaded = Config.load()
    assert loaded.api_port == 9001
    assert loaded.watchdog.memory_limit_mb == 9000


def test_corrupt_config_quarantined_not_fatal(cfg_path: Path):
    cfg_path.write_text("{not json", encoding="utf-8")
    loaded = Config.load()
    assert loaded.api_port == 8212  # defaults
    assert not cfg_path.exists()
    assert cfg_path.with_suffix(".json.broken").exists()


def test_secret_reads_survive_missing_keyring_backend(monkeypatch: pytest.MonkeyPatch):
    # Headless Linux often has no keyring backend; keyring raises instead of
    # returning None. A secret read must degrade to "" — not crash-loop the
    # daemon at startup under systemd.
    import keyring

    def explode(service, name):
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(config_mod.keyring, "get_password", explode)
    assert config_mod.get_admin_password() == ""
    assert config_mod.get_discord_token() == ""
