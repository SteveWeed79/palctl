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
    base = os.environ.get("APPDATA")
    if not base:
        # %APPDATA% is set by the interactive shell, NOT by the service
        # manager — a Windows service (even one running as this user) doesn't
        # have it. The canonical per-user location on Windows is still
        # <profile>\AppData\Roaming, so land exactly where the GUI lands; the
        # old ~/.config fallback was a Linux convention that, on Windows, put
        # a service daemon in a folder no GUI ever reads (two configs, two
        # tokens, 401 on every call). ~/.config remains the non-Windows path.
        home = Path.home()
        base = str(
            home / "AppData" / "Roaming"
            if sys.platform.startswith("win")
            else home / ".config"
        )
    d = Path(base) / "palctl"
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = config_dir() / "config.json"


def _coerce(value: object, default: object) -> object:
    """Convert `value` to the type of `default`, or raise ValueError.

    Every field on these dataclasses defaults to a plain scalar, so the default's
    type *is* the field's type — and reading it off the value avoids parsing the
    string annotations that `from __future__ import annotations` leaves behind.

    bool is checked before int because it is a subclass of one: a `true` landing
    in an int field is a mistake worth rejecting, not a silent 1.
    """
    want = type(default)
    if type(value) is want:
        return value

    if want is bool:
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "on", "1"):
                return True
            if low in ("false", "no", "off", "0"):
                return False
        raise ValueError(f"cannot read {value!r} as a true/false value")

    if want is int:
        if isinstance(value, bool):
            raise ValueError(f"expected a number, got the boolean {value!r}")
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{value!r} is not a finite number")
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return int(float(value.strip()))  # "6.5" -> 6, or raises
        raise ValueError(f"cannot read {value!r} as a number")

    if want is float:
        if isinstance(value, bool):
            raise ValueError(f"expected a number, got the boolean {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.strip())
        raise ValueError(f"cannot read {value!r} as a number")

    if want is str:
        # An unquoted app_id or port is worth accepting; a list or dict where a
        # path belongs is a real mistake and must not become its repr().
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise ValueError(f"cannot read {value!r} as text")

    return value


def _known(cls: type, raw: object, exclude: tuple[str, ...] = ()) -> dict:
    """The subset of `raw` this version understands, converted to the field types.

    Two kinds of junk get filtered here, and neither should reach the daemon:

    Keys written by a different palctl version are dropped — that is the original
    job, and what lets a config survive a downgrade.

    Wrong-typed values are converted where the intent is obvious and otherwise
    dropped, so the field keeps its default. JSON has no schema and this file is
    documented as hand-editable ("set watchdog.auto_restart_on_crash to true"),
    so a quoted number is a routine typo — and dataclasses enforce nothing at
    runtime, so `"12000"` used to sail all the way into the watchdog and fail
    there, comparing str to float on every tick. `poll_seconds` was worse: the
    TypeError landed on the sleep at the bottom of the loop, outside the guard
    that wraps each tick, so the watchdog task died and the memory leak it exists
    to catch went unwatched until someone restarted the daemon. Fixing it at the
    boundary means a bad value costs one field and one log line.
    """
    if not isinstance(raw, dict):
        return {}
    defaults = {
        f.name: f.default for f in fields(cls) if f.name not in exclude  # type: ignore[arg-type]
    }
    out: dict = {}
    for key, value in raw.items():
        if key not in defaults:
            continue
        try:
            out[key] = _coerce(value, defaults[key])
        except (ValueError, TypeError, OverflowError) as e:
            logging.getLogger("palctl.config").warning(
                "config: ignoring %s.%s — %s; using the default %r instead",
                cls.__name__, key, e, defaults[key],
            )
    return out


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

    # Opt-in crash/hang recovery. The service wrapper already restarts a *crashed* process, but
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

    # Frame-time / FPS degradation watchdog. Palworld can bog down to a slideshow
    # while still under the memory limit (the memory watchdog never fires). When
    # the REST API reports server FPS below `min_server_fps` for
    # `fps_consecutive_samples` polls, restart on that symptom too. Opt-in, and 0
    # disables it. A reported FPS of 0 is ignored (server booting / API blip).
    fps_restart: bool = False
    min_server_fps: int = 0
    fps_consecutive_samples: int = 5

    # Low-disk alert. A full disk corrupts saves and crashes the server, and the
    # backup safety net silently fails. Warn (once per episode) when free space on
    # the server or backup volume drops below this. 0 disables. Not a restart
    # trigger — just an alert, since only a human can free space.
    disk_min_free_gb: int = 5


@dataclass
class ScheduleConfig:
    enabled: bool = True
    daily_restart: bool = True
    daily_restart_at: str = "06:00"
    # Restart every N hours instead of once a day at a fixed time. Many servers
    # run a 6-8h cadence to stay ahead of the leak. 0 (default) = keep the
    # daily-at-HH:MM behaviour; > 0 takes precedence over daily_restart_at.
    restart_every_hours: int = 0
    # How long the in-game countdown runs before a restart takes the server
    # down. This used to be a hard-coded ten minutes with no setting at all, so
    # every restart — including "restart it now, nobody's on" — cost ten
    # minutes. 0 = no countdown; capped at an hour (countdown.MAX_SECONDS),
    # since the operation lock is held for the whole wait.
    restart_countdown_seconds: int = 600
    # The same warning before a restore overwrites the world. Restores used to
    # drop everyone the instant the button was clicked, which is also why there
    # was never a window in which to cancel a mis-clicked one. 0 restores the
    # old immediate behaviour.
    restore_countdown_seconds: int = 60
    # Nobody online — or a server whose REST API isn't answering — means the
    # countdown is being announced to an empty room. Collapse it to a few
    # seconds instead of waiting it out. Turn off to always wait the full time.
    skip_countdown_when_empty: bool = True
    autosave_minutes: int = 15
    # Local backups always run — this is only how often. Capped at 24h (the GUI
    # and the scheduler both enforce it) so backups happen at least once a day;
    # the admin can choose anything more frequent.
    backup_hours: int = 6
    backup_retain: int = 24
    # How many backups to keep on the mirror (second copy). Cloud storage costs
    # money, so you may want fewer copies off-site than on the local disk — or,
    # with cheap cold storage, more. 0 = keep the same count as backup_retain.
    mirror_retain: int = 0
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
    admin_role_id: int = 0  # role OR user ID allowed to run /restart, /kick, /ban
    # The one guild allowed to drive this server. 0 = infer it from channel_id's
    # guild; only if there's no channel either does the bot accept commands from
    # anywhere. Slash commands are registered globally and Discord apps are
    # public by default, so without this anyone who has the bot's client ID can
    # invite it to a guild of their own — where they hold Manage Server, which
    # is exactly what admin access falls back to when admin_role_id is unset.
    guild_id: int = 0
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
    # Optional off-site copy of every backup. Local backups always run (they're
    # the safety net); this is the *second*, off-machine copy, because backups on
    # the server's own disk don't survive that disk. Either a local path (another
    # disk or a network share) or an rclone remote for cloud storage, written as
    # `remote:path` — e.g. `gdrive:PalworldBackups` after `rclone config`.
    #
    # `backup_mirror_enabled` is the on/off switch, kept separate from the path so
    # off-site backups can be turned off without losing the configured target —
    # flip it back on later without re-typing the remote. Off by default; the path
    # is only mirrored to when the switch is on AND non-empty.
    backup_mirror: str = ""
    backup_mirror_enabled: bool = False
    service_name: str = "PalServer"
    app_id: str = "2394010"

    # REST API (the Palworld server's own admin API — palctl only ever talks to
    # it on this box, so this stays loopback).
    api_host: str = "127.0.0.1"
    api_port: int = 8212

    # Web dashboard + control API — the daemon's OWN HTTP server (port 8830),
    # the thing `palctl ui` opens and the GUI talks to.
    #   "127.0.0.1" — reachable only from a browser on THIS PC (the safe
    #     default; remote access then rides an SSH tunnel or Tailscale).
    #   "0.0.0.0"   — reachable from other devices on your LAN. The per-user
    #     token in the dashboard URL is then the ONLY credential, and it travels
    #     over plain HTTP, so keep this to a network you trust and never
    #     port-forward the port to the internet.
    # Takes effect when the daemon (re)starts — the socket is bound once at boot.
    ui_bind_host: str = "127.0.0.1"

    poll_seconds: int = 10

    # Check GitHub for a newer palctl on startup (best-effort; just notifies).
    check_for_updates: bool = True

    # A second alert channel besides Discord + the GUI/log, so the daemon can
    # still reach you when Discord is down or unconfigured. One outbound HTTP
    # POST to any URL — an ntfy topic, a Slack/Discord incoming webhook, or your
    # own endpoint. The payload carries the message under `content`/`text`/
    # `message` so the common receivers accept it unchanged. Off by default;
    # fires only when enabled AND the URL is non-empty. The URL is not a secret
    # in the DPAPI sense (it lives in config.json), so treat a webhook URL that
    # embeds a token like any other capability URL.
    alert_webhook_enabled: bool = False
    alert_webhook_url: str = ""

    # How the daemon starts in the background, as last chosen in setup: "login"
    # (HKCU Run key), "service" (Windows service / systemd unit), or "none".
    # "" = setup never recorded a choice (a config from before this field
    # existed); the wizard then falls back to probing what's registered. Only
    # setup writes this — it's what makes a re-run default to the mode the user
    # actually picked instead of silently switching back to login startup.
    daemon_startup: str = ""

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
        cfg = cls(
            **{
                **_known(cls, raw, exclude=("watchdog", "schedule", "discord")),
                "watchdog": WatchdogConfig(**_known(WatchdogConfig, raw.get("watchdog", {}))),
                "schedule": ScheduleConfig(**_known(ScheduleConfig, raw.get("schedule", {}))),
                "discord": DiscordConfig(**_known(DiscordConfig, raw.get("discord", {}))),
            }
        )
        # Back-compat: a config written before the off-site on/off switch existed
        # had a mirror path but no `backup_mirror_enabled` key. A set path used to
        # mean "on", so treat it that way — otherwise an upgrade would silently
        # stop mirroring the backups the user was already sending off-site.
        if "backup_mirror_enabled" not in raw and cfg.backup_mirror:
            cfg.backup_mirror_enabled = True
        return cfg

    @classmethod
    def load(cls) -> Config:
        if not CONFIG_PATH.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            # A corrupt config must not crash-loop the daemon under the service wrapper.
            # Set the file aside so the values can still be recovered by hand —
            # and say so: the daemon silently running on default paths (backups
            # start failing, watchdog thresholds reset) is baffling without
            # this one line naming the root cause.
            logging.getLogger("palctl.config").warning(
                "config.json was unreadable — set aside as config.json.broken; "
                "running with built-in defaults until it is fixed or re-saved"
            )
            # Setting it aside is a courtesy, not the point — the point is to
            # come back with defaults instead of dying. The rename can fail on
            # its own (a Windows AV scanner or the search indexer holding the
            # file open is a PermissionError), and letting that escape would
            # crash the daemon *before* asyncio.run, in the recovery path
            # written to prevent exactly that crash loop.
            try:
                CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".json.broken"))
            except OSError:
                logging.getLogger("palctl.config").warning(
                    "could not rename config.json aside (%s) — leaving it in "
                    "place and continuing on defaults", CONFIG_PATH,
                )
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
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:
        # A *broken* keyring backend (not keyring itself) can fail outside the
        # Exception hierarchy, sailing past the guard above. The one seen in the
        # wild: a system `cryptography` with a missing _cffi_backend makes pyo3
        # raise pyo3_runtime.PanicException, which derives from BaseException.
        # That would kill the daemon before asyncio.run — a crash loop under
        # WinSW/systemd. Reads must never do that (see the module note above), so
        # log the workaround and fall back to "no secret".
        logging.getLogger("palctl.config").warning(
            "keyring backend failed reading %r (%s: %s); treating as no secret. "
            "If the system keyring is broken, set "
            "PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring in the daemon's "
            "environment.",
            name,
            type(e).__name__,
            e,
        )
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
