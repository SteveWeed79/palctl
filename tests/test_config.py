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


# ---------------- config_dir: the same folder for GUI and service ----------------


def test_config_dir_uses_appdata_when_set(tmp_path, monkeypatch):
    import palctl.config as config_mod

    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert config_mod.config_dir() == tmp_path / "Roaming" / "palctl"


def test_config_dir_windows_fallback_is_roaming_not_dot_config(tmp_path, monkeypatch):
    # THE 401 bug: %APPDATA% is an interactive-shell variable — a Windows
    # service (even under your own account) doesn't get it. The fallback must
    # land exactly where the GUI lands (<profile>\AppData\Roaming), never in a
    # Linux-style ~/.config the GUI will never read — that split produced two
    # tokens and "the daemon rejected the token" on every call.
    import palctl.config as config_mod

    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(config_mod.sys, "platform", "win32")
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    assert config_mod.config_dir() == tmp_path / "AppData" / "Roaming" / "palctl"


def test_config_dir_linux_fallback_stays_dot_config(tmp_path, monkeypatch):
    import palctl.config as config_mod

    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(config_mod.sys, "platform", "linux")
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    assert config_mod.config_dir() == tmp_path / ".config" / "palctl"


def test_a_corrupt_config_survives_a_failed_quarantine(tmp_path, monkeypatch):
    """Quarantining the bad file is a courtesy; coming back with defaults is the
    point. The rename can fail on its own — a Windows AV scanner or the search
    indexer holding config.json open raises PermissionError — and letting that
    escape kills the daemon *before* asyncio.run, in the very recovery path
    written to stop it crash-looping under the service wrapper."""
    from pathlib import Path

    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)

    real_replace = Path.replace

    def refuse(self, target):
        if self.name == "config.json":
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", refuse)

    cfg = Config.load()
    assert isinstance(cfg, Config)  # defaults, not a crash
    assert path.exists(), "the unreadable file is left in place for the user"


# ---------------- wrong-typed values in a hand-edited config ----------------
#
# config.json is documented as hand-editable ("set watchdog.auto_restart_on_crash
# to true"), JSON has no schema, and dataclasses enforce nothing at runtime. A
# quoted number therefore used to load clean and fail much later, in the worker
# that consumed it — `poll_seconds` as a string killed the whole watchdog task,
# because the TypeError landed on the sleep at the bottom of the loop rather
# than inside the guard that wraps each tick.


def test_quoted_numbers_are_read_as_numbers(cfg_path: Path):
    cfg_path.write_text(
        json.dumps(
            {
                "poll_seconds": "10",
                "watchdog": {"memory_limit_mb": "12000", "poll_seconds": "60"},
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.poll_seconds == 10
    assert cfg.watchdog.memory_limit_mb == 12_000
    # The two operations that used to raise TypeError on a live daemon.
    assert max(1, cfg.watchdog.poll_seconds) == 60
    assert 5_000.0 < cfg.watchdog.memory_limit_mb


def test_stringy_booleans_are_read_as_booleans(cfg_path: Path):
    cfg_path.write_text(
        json.dumps(
            {
                "check_for_updates": "false",
                "watchdog": {"auto_restart_on_crash": "true"},
                "schedule": {"auto_update": "no"},
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.check_for_updates is False
    assert cfg.watchdog.auto_restart_on_crash is True
    assert cfg.schedule.auto_update is False


def test_unquoted_ids_and_paths_are_read_as_text(cfg_path: Path):
    """app_id is a string field holding digits, so writing it unquoted is a
    natural mistake; a Discord snowflake pasted as a string is the mirror case."""
    cfg_path.write_text(
        json.dumps({"app_id": 2394010, "discord": {"channel_id": "123456789012345678"}}),
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.app_id == "2394010"
    assert cfg.discord.channel_id == 123456789012345678


def test_junk_values_fall_back_to_the_default(cfg_path: Path):
    """Where the intent isn't recoverable the field keeps its default — one bad
    value must not cost the rest of the file, and a list must never become a
    path via its repr()."""
    cfg_path.write_text(
        json.dumps(
            {
                "api_port": 9001,
                "server_root": ["not", "a", "path"],
                "watchdog": {"hard_limit_mb": "not a number", "memory_limit_mb": 9000},
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.server_root == Config().server_root
    assert cfg.watchdog.hard_limit_mb == Config().watchdog.hard_limit_mb
    # ...and the good values around it still land.
    assert cfg.api_port == 9001
    assert cfg.watchdog.memory_limit_mb == 9000


def test_a_boolean_in_a_number_field_is_rejected(cfg_path: Path):
    """bool is a subclass of int, so `true` would otherwise become a silent 1 —
    a memory limit of 1 MB restarts the server on every single poll."""
    cfg_path.write_text(
        json.dumps({"watchdog": {"memory_limit_mb": True}}), encoding="utf-8"
    )
    assert Config.load().watchdog.memory_limit_mb == Config().watchdog.memory_limit_mb


def test_a_wrong_typed_section_does_not_quarantine_the_file(cfg_path: Path):
    """A nested section that isn't an object used to raise AttributeError inside
    from_dict, which load() treats as a corrupt file — so one bad key threw away
    every setting in it."""
    cfg_path.write_text(
        json.dumps({"api_port": 9001, "schedule": None, "discord": "nope"}),
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.api_port == 9001  # the rest of the file survived
    assert cfg.schedule.backup_hours == Config().schedule.backup_hours
    assert not cfg_path.with_suffix(".json.broken").exists()
