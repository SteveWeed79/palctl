"""The first-run setup orchestration (palctl/setup_flow.py), extracted from the
wizard's QThread so it can be tested without Qt or Windows. This is the most
consequential sequence in the app — preflight, save config, SteamCMD install
with its guards, enable the REST API, register services, verify — and it used to
have no coverage at all. Every external effect is faked, the way
test_scheduler_ops.py fakes subprocesses.

Skips where the daemon's own deps (aiohttp/discord, pulled in by
palctl.daemon) aren't installed."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")   # palctl.daemon (imported lazily by setup_flow)
pytest.importorskip("discord")

import palctl.config as config_mod  # noqa: E402
from palctl.config import Config  # noqa: E402
from palctl.preflight import Check  # noqa: E402
from palctl.setup_flow import SetupPlan, run_setup  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect config to a temp file and stub every external effect setup_flow
    reaches. Returns a namespace of recorders plus a `run(plan)` helper that
    returns (result, log_lines)."""
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("palctl.setup_flow.config_dir", lambda: tmp_path)

    rec = types.SimpleNamespace(
        admin_passwords=[],
        discord_tokens=[],
        services_registered=[],
        server_service_kwargs=None,   # user=/password= the game service registered with
        daemon_service=0,
        daemon_service_as_user=None,  # how the daemon service was registered
        daemon_service_password=None,
        login_startup=0,
        startup_disabled=0,
        rest_api=[],
        run_update_calls=0,
        backups_created=[],
        stops=[],
        starts=[],
        find_process=lambda: None,   # server not running by default
        default_ini_exists=True,     # SteamCMD "succeeded" by default
        is_blank=False,
        run_update=None,             # set to an exception to make it raise
    )

    # Secrets.
    monkeypatch.setattr("palctl.setup_flow.set_admin_password", rec.admin_passwords.append)
    monkeypatch.setattr("palctl.setup_flow.set_discord_token", rec.discord_tokens.append)
    # An existing steamcmd, so the install path never tries to download one.
    monkeypatch.setattr("palctl.setup_flow.is_steamcmd", lambda p: True)

    # Preflight passes (no ❌) by default.
    monkeypatch.setattr("palctl.preflight.run_all", lambda *a, **k: [])
    monkeypatch.setattr("palctl.preflight.install_vcredist", lambda on_line=None: 0)

    # REST API enable.
    monkeypatch.setattr(
        "palctl.serversetup.ensure_rest_api",
        lambda *a, **k: rec.rest_api.append((a, k)),
    )

    # SteamCMD.
    def _run_update(steam, root, app_id=None, on_line=None):
        rec.run_update_calls += 1
        if rec.run_update is not None:
            raise rec.run_update
        # Materialise the "server files present" artifact the flow checks for.
        if rec.default_ini_exists:
            Path(root).mkdir(parents=True, exist_ok=True)
            (Path(root) / "DefaultPalWorldSettings.ini").write_text("x", encoding="utf-8")
        return 0

    monkeypatch.setattr("palctl.steamcmd.run_update", _run_update)
    monkeypatch.setattr("palctl.steamcmd.backup_file", lambda p: None)
    monkeypatch.setattr("palctl.steamcmd.parse_progress", lambda line: None)
    monkeypatch.setattr("palctl.inifile.is_blank", lambda p: rec.is_blank)

    # Process control (async).
    monkeypatch.setattr("palctl.procs.find_process", lambda: rec.find_process())

    async def _stop(name):
        rec.stops.append(name)
        return True

    async def _start(name):
        rec.starts.append(name)
        return True

    monkeypatch.setattr("palctl.procs.stop_service", _stop)
    monkeypatch.setattr("palctl.procs.start_service", _start)

    # World backup.
    def _backup(src, dst, tag):
        rec.backups_created.append(tag)
        return types.SimpleNamespace(path=Path(dst) / f"{tag}-backup")

    monkeypatch.setattr("palctl.backups.create", _backup)

    # Service registration.
    monkeypatch.setattr("palctl.winservice.ensure_winsw", lambda d: tmp_path / "winsw.exe")

    def _winservice_install(*a, **k):
        rec.services_registered.append(a)
        rec.server_service_kwargs = k  # capture user=/password= for Path A checks

    monkeypatch.setattr("palctl.winservice.install_service", _winservice_install)

    def _daemon_install_service(as_user=False, password=None):
        rec.daemon_service += 1
        rec.daemon_service_as_user = as_user
        rec.daemon_service_password = password
        return True  # verified-up, like the real one reports

    def _daemon_install_startup():
        rec.login_startup += 1

    def _daemon_disable_background():
        rec.startup_disabled += 1

    monkeypatch.setattr("palctl.daemon.install_service", _daemon_install_service)
    monkeypatch.setattr("palctl.daemon.install_startup", _daemon_install_startup)
    monkeypatch.setattr("palctl.daemon.start_detached", lambda: True)
    monkeypatch.setattr(
        "palctl.daemon.disable_background_startup", _daemon_disable_background
    )
    # No daemon service registered by default (needs_admin probes for one).
    monkeypatch.setattr("palctl.winservice.service_exists", lambda name: False)

    # Verify step: a server that answers, no network lookups.
    class _FakeApi:
        def __init__(self, host, port, password):
            pass

        async def wait_until_alive(self, timeout=240):
            return True

    monkeypatch.setattr("palctl.api.PalApi", _FakeApi)
    monkeypatch.setattr("palctl.netinfo.lan_ip", lambda: "")
    monkeypatch.setattr("palctl.netinfo.public_ip", lambda: "")

    def run(plan):
        lines = []
        result = run_setup(Config(), plan, lines.append)
        return result, lines

    rec.tmp_path = tmp_path
    rec.run = run
    return rec


def _plan(tmp_path, **over):
    server_root = str(tmp_path / "server")
    base = dict(
        server_root=server_root,
        steamcmd_path=str(tmp_path / "steamcmd.exe"),
        api_port=8212,
        password="secret",
        install_server=False,
        install_vcredist=False,
        register_server_service=False,
        daemon_startup="none",
        service_name="PalServer",
        backup_root=str(tmp_path / "backups"),
        backup_hours=6,
    )
    base.update(over)
    return SetupPlan(**base)


# ---------------- config persistence & plan permutations ----------------


def test_minimal_run_saves_config_and_reports_success(env):
    plan = _plan(env.tmp_path, api_port=9001, backup_hours=12)
    result, _ = env.run(plan)
    assert result.ok is True
    assert result.server_registered is False
    assert env.admin_passwords == ["secret"]

    saved = Config.load()
    assert saved.server_root == plan.server_root
    assert saved.api_port == 9001
    assert saved.backup_root == plan.backup_root
    assert saved.schedule.backup_hours == 12
    assert saved.discord.enabled is False


@pytest.mark.parametrize(
    "startup, expect_service, expect_login, expect_disabled",
    [("none", 0, 0, 1), ("service", 1, 0, 0), ("login", 0, 1, 0)],
)
def test_daemon_startup_permutations(
    env, startup, expect_service, expect_login, expect_disabled
):
    result, _ = env.run(_plan(env.tmp_path, daemon_startup=startup))
    assert result.ok is True
    assert env.daemon_service == expect_service
    assert env.login_startup == expect_login
    # "none" is not "leave it alone": unticking the background group on a
    # re-run must actually remove the previous run's autostart (same contract
    # as the Discord toggle).
    assert env.startup_disabled == expect_disabled
    # The choice is persisted, so a wizard re-run defaults to what the user
    # actually picked instead of silently switching back to login startup.
    assert Config.load().daemon_startup == startup


def test_service_mode_with_password_registers_both_under_the_user(env):
    # Path A: "service" mode + a Windows password registers BOTH the daemon and
    # the game server under the invoking user, so palctl can read the server it
    # watches (and its DPAPI secrets) instead of the SYSTEM/user split that
    # silently blinds the watchdog.
    plan = _plan(
        env.tmp_path,
        daemon_startup="service",
        register_server_service=True,
        service_password="hunter2",
    )
    server_root = Path(plan.server_root)
    server_root.mkdir(parents=True, exist_ok=True)
    (server_root / "PalServer.exe").write_text("x", encoding="utf-8")

    result, _ = env.run(plan)
    assert result.ok is True
    # Daemon: registered as the user, password passed straight through (no prompt).
    assert env.daemon_service_as_user is True
    assert env.daemon_service_password == "hunter2"
    # Game service: registered under a user account too.
    assert env.server_service_kwargs.get("user")  # .\<username>
    assert env.server_service_kwargs.get("password") == "hunter2"


def test_service_mode_without_password_stays_localsystem(env):
    # No password → the old LocalSystem behaviour is preserved (the daemon's
    # runtime account-mismatch warning then nudges the user toward Path A).
    plan = _plan(
        env.tmp_path,
        daemon_startup="service",
        register_server_service=True,
    )
    server_root = Path(plan.server_root)
    server_root.mkdir(parents=True, exist_ok=True)
    (server_root / "PalServer.exe").write_text("x", encoding="utf-8")

    result, _ = env.run(plan)
    assert result.ok is True
    assert env.daemon_service_as_user is False
    assert env.server_service_kwargs.get("user") is None


# ---------------- elevation requirements ----------------


def test_needs_admin_for_any_service_registration():
    from palctl.setup_flow import needs_admin

    assert needs_admin(register_server_service=True, daemon_startup="none") is True
    assert needs_admin(register_server_service=False, daemon_startup="service") is True


def test_needs_admin_to_remove_a_registered_daemon_service(monkeypatch):
    # Switching AWAY from a daemon service (to login startup or none) has to
    # unregister it — which needs elevation too, or the removal fails silently
    # and the old daemon keeps running.
    import palctl.winservice as winservice
    from palctl.setup_flow import needs_admin

    monkeypatch.setattr(winservice, "service_exists", lambda name: True)
    assert needs_admin(register_server_service=False, daemon_startup="login") is True
    assert needs_admin(register_server_service=False, daemon_startup="none") is True

    monkeypatch.setattr(winservice, "service_exists", lambda name: False)
    assert needs_admin(register_server_service=False, daemon_startup="login") is False


def test_discord_section_writes_token_channel_and_role(env):
    plan = _plan(
        env.tmp_path,
        setup_discord=True,
        discord_token="tok",
        discord_channel_id=123,
        discord_admin_id=456,
    )
    result, _ = env.run(plan)
    assert result.ok is True
    assert env.discord_tokens == ["tok"]
    saved = Config.load()
    assert saved.discord.enabled is True
    assert saved.discord.channel_id == 123
    assert saved.discord.admin_role_id == 456


def test_discord_unticked_disables_and_never_writes_a_token(env):
    result, _ = env.run(_plan(env.tmp_path, setup_discord=False))
    assert result.ok is True
    assert env.discord_tokens == []          # token untouched
    assert Config.load().discord.enabled is False


def test_blocking_preflight_aborts_before_saving_anything(env, monkeypatch):
    monkeypatch.setattr(
        "palctl.preflight.run_all",
        lambda *a, **k: [Check("Disk space", False, "full", fix="free space")],
    )
    result, lines = env.run(_plan(env.tmp_path, install_server=True))
    assert result.ok is False
    assert env.admin_passwords == []                 # never got to saving
    assert not (env.tmp_path / "config.json").exists()
    assert any("run setup again" in ln for ln in lines)


# ---------------- service registration ----------------


def test_service_registration_registers_when_the_exe_exists(env):
    server_root = Path(env.tmp_path / "server")
    server_root.mkdir(parents=True)
    (server_root / "PalServer.exe").write_text("MZ", encoding="utf-8")

    result, _ = env.run(
        _plan(env.tmp_path, register_server_service=True, service_name="PalSvc")
    )
    assert result.ok is True
    assert result.server_registered is True
    assert len(env.services_registered) == 1
    assert env.starts == ["PalSvc"]  # verify step started it


def test_service_registration_skipped_when_the_exe_is_missing(env):
    # register requested, but PalServer.exe isn't there (a partial install).
    result, lines = env.run(_plan(env.tmp_path, register_server_service=True))
    assert result.ok is True
    assert result.server_registered is False
    assert env.services_registered == []
    assert env.starts == []  # nothing to start/verify
    assert any("skipping the server service" in ln for ln in lines)


# ---------------- the SteamCMD install path & its guards ----------------


def test_install_backs_up_the_world_and_succeeds(env):
    plan = _plan(env.tmp_path, install_server=True)
    # A pre-existing world so the pre-update backup branch runs.
    savegames = Config(server_root=plan.server_root).savegames_dir
    savegames.mkdir(parents=True)

    result, _ = env.run(plan)
    assert result.ok is True
    assert env.run_update_calls == 1
    assert env.backups_created == ["pre-update"]


def test_install_aborts_when_the_server_wont_stop(env):
    # A server that stays alive after the stop must abort the update, untouched.
    env.find_process = lambda: types.SimpleNamespace(pid=1)  # never dies
    result, lines = env.run(_plan(env.tmp_path, install_server=True))
    assert result.ok is False
    assert env.run_update_calls == 0                  # never rewrote files
    assert env.stops == ["PalServer"]                 # it tried the graceful stop
    assert any("still running" in ln for ln in lines)


def test_install_aborts_when_server_files_are_absent_after_steamcmd(env):
    # SteamCMD's exit code lies; the flow checks the artifact and aborts with the
    # real reason instead of a misleading REST-API error a step later.
    env.default_ini_exists = False
    result, lines = env.run(_plan(env.tmp_path, install_server=True))
    assert result.ok is False
    assert env.services_registered == []              # never reached registration
    assert any("aren't present after SteamCMD" in ln for ln in lines)


def test_blanked_ini_is_restored_even_when_steamcmd_raises(env, monkeypatch):
    plan = _plan(env.tmp_path, install_server=True)
    cfg = Config(server_root=plan.server_root)
    live_ini = cfg.live_ini
    live_ini.parent.mkdir(parents=True, exist_ok=True)
    live_ini.write_text("blanked", encoding="utf-8")

    backup = env.tmp_path / "ini.bak"
    backup.write_text("GOOD SETTINGS", encoding="utf-8")
    monkeypatch.setattr("palctl.steamcmd.backup_file", lambda p: backup)
    env.is_blank = True                       # ini looks blanked after the run
    env.run_update = RuntimeError("steam boom")  # ...and the update then dies

    result, lines = env.run(plan)
    assert result.ok is False
    # The finally block restored the tuned ini from the pre-update backup.
    assert live_ini.read_text(encoding="utf-8") == "GOOD SETTINGS"
    assert any("restored it from the pre-update backup" in ln for ln in lines)
