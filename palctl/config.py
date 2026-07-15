"""
App config + secrets.

Everything is set through the UI. Nothing is hand-edited, and no secret ever
touches disk in the clear.

Non-secret config -> %APPDATA%/palctl/config.json
Secrets (admin password, Discord bot token) -> Windows Credential Manager via
`keyring`, which on Windows is DPAPI-backed and encrypted against your user
account. A leaked bot token means somebody else's code runs as your bot, so
plaintext in a config file is not acceptable.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import keyring

SERVICE_ID = "palctl"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "palctl"
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = config_dir() / "config.json"


def _known(cls: type, raw: dict, exclude: tuple[str, ...] = ()) -> dict:
    """Drop keys written by a different palctl version that this one doesn't know."""
    names = {f.name for f in fields(cls)} - set(exclude)  # type: ignore[arg-type]
    return {k: v for k, v in raw.items() if k in names}


@dataclass
class WatchdogConfig:
    enabled: bool = True
    # Palworld's memory leak is well documented. Restart on bloat, not on a guess.
    memory_limit_mb: int = 12_000
    # Don't restart on a single spike — require N consecutive samples over the line.
    consecutive_samples: int = 3
    # Never restart while people are mid-session unless it's really bad.
    skip_if_players_online: bool = True
    hard_limit_mb: int = 16_000  # override skip_if_players_online above this
    warn_seconds: int = 300  # in-game countdown before the restart
    poll_seconds: int = 60

    # Opt-in crash/hang recovery. NSSM already restarts a *crashed* process, but
    # it can't fix a server that's still running yet has stopped answering (a
    # hang). If the REST API is unreachable for `crash_confirm_polls` polls while
    # palctl itself didn't stop the server, bring it back — rate-limited so a
    # genuine crash-loop doesn't get hammered.
    auto_restart_on_crash: bool = False
    crash_confirm_polls: int = 3
    crash_restart_max_per_hour: int = 3

    # Leak forecasting: fit the recent memory growth and act *before* the
    # limit, instead of reacting at the threshold with players mid-session.
    predict_notify: bool = True  # warn once when the limit is < horizon away
    preempt_restart: bool = False  # opt-in: restart early while the server is empty
    preempt_horizon_minutes: int = 90


@dataclass
class ScheduleConfig:
    enabled: bool = True
    daily_restart: bool = True
    daily_restart_at: str = "06:00"
    autosave_minutes: int = 15
    backup_hours: int = 6
    backup_retain: int = 24
    # Palworld patches constantly. Opt-in: run a SteamCMD update at a quiet hour,
    # reusing the same stop -> backup -> update -> restart flow as manual updates.
    auto_update: bool = False
    auto_update_at: str = "05:00"
    # Game updates are exactly when saves get eaten, so by default a failed
    # pre-update backup ABORTS the update — a server one patch behind beats an
    # updated server whose world can't be rolled back. Opt out only if you'd
    # rather the update always proceed (e.g. backups live on a flaky share).
    update_requires_backup: bool = True


@dataclass
class DiscordConfig:
    enabled: bool = False
    channel_id: int = 0
    admin_role_id: int = 0  # who may run /restart, /kick, /ban
    notify_join_leave: bool = True
    notify_level_up: bool = True
    notify_watchdog: bool = True
    notify_server_up_down: bool = True
    notify_update_available: bool = True
    # A single embed that refreshes in place with live status, instead of spam.
    status_message: bool = False
    # Sent to the channel when a player joins, if set. "{name}" is filled in.
    welcome_message: str = ""


@dataclass
class Config:
    # Paths
    server_root: str = r"C:\steamcmd\steamapps\common\PalServer"
    steamcmd_path: str = r"C:\steamcmd\steamcmd.exe"
    backup_root: str = r"D:\PalworldBackups"
    # Optional second copy of every backup. Backups on the server's own disk
    # don't survive the disk. Either a local path (another disk or a network
    # share) or an rclone remote for off-site/cloud storage, written as
    # `remote:path` — e.g. `gdrive:PalworldBackups` after `rclone config`.
    # Empty = off.
    backup_mirror: str = ""
    service_name: str = "PalServer"
    app_id: str = "2394010"

    # REST API
    api_host: str = "127.0.0.1"
    api_port: int = 8212

    poll_seconds: int = 10

    # Check GitHub for a newer palctl on startup (best-effort; just notifies).
    check_for_updates: bool = True

    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)

    # ---------- derived paths ----------

    @property
    def saved_dir(self) -> Path:
        return Path(self.server_root) / "Pal" / "Saved"

    @property
    def savegames_dir(self) -> Path:
        return self.saved_dir / "SaveGames"

    @property
    def live_ini(self) -> Path:
        # The server writes its live ini under WindowsServer/ or LinuxServer/
        # depending on the server's OS — which is this box's OS, since palctl
        # runs on the same machine as the server.
        sub = "WindowsServer" if sys.platform.startswith("win") else "LinuxServer"
        return self.saved_dir / "Config" / sub / "PalWorldSettings.ini"

    @property
    def default_ini(self) -> Path:
        return Path(self.server_root) / "DefaultPalWorldSettings.ini"

    # ---------- persistence ----------

    @classmethod
    def from_dict(cls, raw: dict) -> Config:
        """Build a Config from a raw dict, dropping keys from other versions and
        rebuilding the nested dataclasses. Shared by load() and profiles."""
        return cls(
            **{
                **_known(cls, raw, exclude=("watchdog", "schedule", "discord")),
                "watchdog": WatchdogConfig(**_known(WatchdogConfig, raw.get("watchdog", {}))),
                "schedule": ScheduleConfig(**_known(ScheduleConfig, raw.get("schedule", {}))),
                "discord": DiscordConfig(**_known(DiscordConfig, raw.get("discord", {}))),
            }
        )

    @classmethod
    def load(cls) -> Config:
        if not CONFIG_PATH.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            # A corrupt config must not crash-loop the daemon under NSSM.
            # Set the file aside so the values can still be recovered by hand —
            # and say so: the daemon silently running on default paths (backups
            # start failing, watchdog thresholds reset) is baffling without
            # this one line naming the root cause.
            logging.getLogger("palctl.config").warning(
                "config.json was unreadable — set aside as config.json.broken; "
                "running with built-in defaults until it is fixed or re-saved"
            )
            CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".json.broken"))
            return cls()

    def save(self) -> None:
        # Write-then-rename so a crash/power-loss mid-write can't leave a
        # truncated config.json — load() would quarantine it and silently revert
        # every path/port/service name to the built-in defaults.
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)


# ---------------- secrets ----------------
# Stored in Windows Credential Manager (DPAPI). Never written to config.json.
#
# Reads must never crash the daemon: a headless Linux box often has no keyring
# backend at all (no DBus/SecretService), and keyring raises NoKeyringError
# rather than returning nothing. Missing secret == empty string; the REST API
# then rejects the empty password, which is a visible, recoverable failure.
# Writes still raise — someone actively saving a secret must see the error.


def _get_secret(name: str) -> str:
    try:
        return keyring.get_password(SERVICE_ID, name) or ""
    except keyring.errors.KeyringError:
        return ""


def set_admin_password(password: str) -> None:
    keyring.set_password(SERVICE_ID, "admin_password", password)


def get_admin_password() -> str:
    return _get_secret("admin_password")


def set_discord_token(token: str) -> None:
    keyring.set_password(SERVICE_ID, "discord_token", token)


def get_discord_token() -> str:
    return _get_secret("discord_token")


def clear_secret(name: str) -> None:
    try:
        keyring.delete_password(SERVICE_ID, name)
    except keyring.errors.KeyringError:
        # Covers both "no such secret" and "no keyring backend at all".
        pass
