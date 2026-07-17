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
    # The dashboard stays loopback-only until the admin opts into LAN access.
    assert cfg.ui_bind_host == "127.0.0.1"


def test_save_load_round_trip(cfg_path: Path):
    cfg = Config()
    cfg.api_port = 9000
    cfg.ui_bind_host = "0.0.0.0"
    cfg.watchdog.memory_limit_mb = 10_000
    cfg.discord.channel_id = 42
    cfg.save()

    loaded = Config.load()
    assert loaded.api_port == 9000
    assert loaded.ui_bind_host == "0.0.0.0"
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


def test_pre_toggle_config_with_a_mirror_path_stays_enabled(cfg_path: Path):
    # Back-compat: a config written before the off-site on/off switch existed had
    # a mirror path but no `backup_mirror_enabled` key. A set path used to mean
    # "on", so an upgrade must keep mirroring — not silently stop.
    cfg = Config.from_dict({"backup_mirror": "gdrive:PalworldBackups"})
    assert cfg.backup_mirror == "gdrive:PalworldBackups"
    assert cfg.backup_mirror_enabled is True


def test_config_without_a_mirror_defaults_off_site_off(cfg_path: Path):
    cfg = Config.from_dict({"api_port": 9000})
    assert cfg.backup_mirror == ""
    assert cfg.backup_mirror_enabled is False


def test_explicit_off_site_disable_is_respected_even_with_a_path(cfg_path: Path):
    # An explicit False must win — disabling off-site backups keeps the path but
    # stops the copying, and reloading the config must not flip it back on.
    cfg = Config.from_dict(
        {"backup_mirror": "gdrive:PalworldBackups", "backup_mirror_enabled": False}
    )
    assert cfg.backup_mirror == "gdrive:PalworldBackups"
    assert cfg.backup_mirror_enabled is False


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


def test_secret_reads_survive_a_backend_panic(monkeypatch: pytest.MonkeyPatch):
    # A broken system keyring backend (e.g. cryptography with a missing
    # _cffi_backend) makes pyo3 raise a PanicException that derives from
    # BaseException, not Exception — so it escapes the KeyringError guard and
    # would kill the daemon before asyncio.run. A read must still degrade to "".
    class FakePanic(BaseException):
        pass

    def panic(service, name):
        raise FakePanic("cffi backend missing")

    monkeypatch.setattr(config_mod.keyring, "get_password", panic)
    assert config_mod.get_admin_password() == ""
    assert config_mod.get_discord_token() == ""


def test_secret_reads_still_propagate_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch):
    # The broad BaseException guard must not swallow real control-flow signals.
    def interrupt(service, name):
        raise KeyboardInterrupt

    monkeypatch.setattr(config_mod.keyring, "get_password", interrupt)
    with pytest.raises(KeyboardInterrupt):
        config_mod.get_admin_password()
