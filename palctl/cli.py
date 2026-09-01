"""
palctl — the terminal client for the daemon.

The GUI is Windows-first; this is for everyone else: headless Linux boxes,
ssh sessions, cron jobs, or just a preference for terminals. Every command
talks to the daemon's token-gated localhost API, so nothing here duplicates
daemon logic — if the daemon can do it, the CLI can trigger it.
"""

from __future__ import annotations

import argparse
import sys

from . import localauth, procs, saveaudit
from .client import DAEMON_PORT, DaemonClient, DaemonError

# ---------------- formatting (pure, tested) ----------------


def _fmt_uptime(seconds: float) -> str:
    m, _ = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m:02d}m" if d else f"{h}h {m:02d}m"


def fmt_countdown(cd: dict) -> str:
    """The one line that tells an admin a restart/restore is pending and what
    they can do about it. Without it, `palctl status` said only 'restart in
    progress' for ten minutes with no clue that it hadn't happened yet."""
    left = int(cd.get("seconds_remaining") or 0)
    clock = f"{left // 60}m {left % 60:02d}s" if left >= 60 else f"{left}s"
    line = f"{cd.get('reason') or cd.get('kind', 'operation')} in {clock}"
    if cd.get("cancellable"):
        line += "  (`palctl cancel` to abort, `palctl skip` to go now)"
    return line


def fmt_status(state: dict) -> str:
    lines = []
    alive = state.get("alive")
    api = "REST API answering" if alive else "REST API not answering"
    lines.append(f"server     {state.get('service', 'UNKNOWN')} ({api})")
    if state.get("operation"):
        lines.append(f"operation  {state['operation']} in progress")
    cd = state.get("countdown")
    if cd:
        lines.append(f"countdown  {fmt_countdown(cd)}")

    m = state.get("metrics")
    if m:
        lines.append(f"players    {m['current_players']}/{m['max_players']}")
        lines.append(f"fps        {m['server_fps']} ({m['server_frame_time']:.1f} ms frame time)")
        lines.append(f"uptime     {_fmt_uptime(m['uptime'])}")
        lines.append(f"in-game    day {m['days']}, {m['base_camps']} base camps")

    p = state.get("process")
    if p:
        lines.append(f"memory     {p['memory_mb']:,.0f} MB")
        lines.append(
            "cpu        "
            + procs.format_cpu(
                p.get("cpu_cores", 0.0),
                p.get("cpu_percent", 0.0),
                measured_launcher=bool(p.get("measured_launcher")),
            )
        )
        if p.get("instances", 1) > 1:
            # These numbers describe ONE of them. Saying which is impossible;
            # saying that there is more than one is the part that matters.
            lines.append(
                f"           ⚠ {p['instances']} Palworld server processes are "
                "running — the readings above are from the largest one"
            )
    return "\n".join(lines)


def fmt_players(players: list[dict]) -> str:
    if not players:
        return "Nobody online."
    header = f"{'NAME':<24} {'LEVEL':>5} {'PING':>6} {'BUILDINGS':>9}"
    rows = [
        f"{p['name']:<24} {p['level']:>5} {p['ping']:>5.0f}ms {p['building_count']:>9}"
        for p in players
    ]
    return "\n".join([header, *rows])


def fmt_backups(backups: list[dict]) -> str:
    if not backups:
        return "No backups yet."
    return "\n".join(f"{b['name']}  ({b['size_mb']:.0f} MB)" for b in backups)


def fmt_events(events: list[dict], n: int = 20) -> str:
    if not events:
        return "No recent events."
    return "\n".join(
        f"{e['at'][:19]}  {e['kind']:<16} {e['message']}{_by(e)}"
        for e in events[-n:]
    )


def _by(event: dict) -> str:
    """" — by zoe (discord)" when a person asked for this, else nothing.

    Absence is meaningful: no attribution means palctl decided it by itself,
    which is exactly what someone reading "the server restarted" needs to know
    first."""
    actor = (event.get("actor") or "").strip()
    via = (event.get("via") or "").strip()
    if not actor and not via:
        return ""
    if actor and via:
        return f"  — by {actor} ({via})"
    return f"  — by {actor or via}"


def _save_audit(days: int, *, deep: bool = False) -> str:
    """Read the world folder and the session history directly.

    Deliberately NOT through the daemon: this is a read-only look at files on
    this machine, it needs no privileges the user doesn't already have, and it
    has to work on exactly the box where a stalling server is being diagnosed —
    including when the daemon is the thing that has fallen over.
    """
    from .config import Config
    from .events import SessionStore

    cfg = Config.load()
    worlds = saveaudit.world_dirs(cfg.savegames_dir)
    if not worlds:
        return (
            f"No world found under {cfg.savegames_dir}. Check the server root "
            "in Config — a world folder holds Level.sav."
        )

    store = SessionStore()
    try:
        seen = store.last_seen_by_player_id()
    finally:
        store.close()

    reports = []
    for w in worlds:
        report = saveaudit.format_audit(saveaudit.audit(w, seen), days=days)
        if deep:
            report += "\n" + _deep_report(w, seen, days)
        reports.append(report)
    return "\n\n".join(reports)


def _deep_report(world, seen: dict, days: int) -> str:
    """The Level.sav half: what those idle players actually weigh.

    Printed as a continuation of the audit rather than replacing it, because a
    failed scan must still leave the operator with the file-size report they
    would have had anyway.
    """
    from . import savescan

    print("Reading Level.sav — this can take minutes on a large world…", flush=True)
    result = savescan.scan(world / saveaudit.LEVEL_SAV)
    if not result.ok:
        return f"\n  Level.sav not read: {result.error}"

    lines = [
        f"\n  Level.sav holds {result.characters:,} character record(s) "
        f"({result.unowned:,} wild or unattributed) and {result.guilds:,} guild(s)."
    ]
    idle = {p.player_id.replace("-", "").upper(): p for p in
            saveaudit.audit(world, seen).inactive(days=days)}
    owed = [(result.by_player.get(g, 0), p) for g, p in idle.items()]
    owed = [(n, p) for n, p in owed if n]
    if owed:
        lines.append("  Of those, belonging to players not seen in months:")
        for n, p in sorted(owed, reverse=True, key=lambda t: t[0]):
            lines.append(f"    {p.name or p.player_id:<24} {n:>6,} record(s)")
        lines.append(
            f"  That is {sum(n for n, _ in owed):,} of {result.characters:,} "
            "records held for players who have stopped playing."
        )
    return "\n".join(lines)


def _save_prune(days: int, *, apply: bool) -> int:
    """Plan — and with --apply, carry out — a Level.sav prune.

    Whether the server is stopped is established by looking for its process,
    not by asking the daemon or trusting a flag. This is the one command that
    rewrites a world, and "the service says STOPPED" is a weaker claim than
    "there is no PalServer process running".
    """
    from pathlib import Path

    from . import procs, saveprune
    from .config import Config
    from .events import SessionStore

    cfg = Config.load()
    worlds = saveaudit.world_dirs(cfg.savegames_dir)
    if not worlds:
        print(f"No world found under {cfg.savegames_dir}.")
        return 1
    if len(worlds) > 1:
        print(
            f"{len(worlds)} worlds found under {cfg.savegames_dir}. palctl will "
            "not guess which one to rewrite — move the others aside first."
        )
        return 1

    running = procs.find_process(cfg.server_root) is not None
    store = SessionStore()
    try:
        seen = store.last_seen_by_player_id()
    finally:
        store.close()

    if apply:
        print("Reading Level.sav and taking a verified backup — this takes a while…",
              flush=True)
    outcome = saveprune.run_prune(
        worlds[0],
        cfg.savegames_dir,
        Path(cfg.backup_root),
        seen,
        server_stopped=not running,
        apply=apply,
        days=days,
    )
    print(saveprune.format_plan(outcome.plan, applied=outcome.applied))
    if outcome.error:
        print(f"\n{outcome.error}")
    if outcome.message:
        print(f"\n{outcome.message}")
    if not apply and outcome.plan.safe:
        print("\nRe-run with --apply to do it, with the server stopped.")
    return 0 if outcome.ok else 1


def find_players(players: list[dict], name: str) -> list[dict]:
    """All online players matching an in-game name (case-insensitive) — the
    daemon's kick/ban actions want the user_id, which nobody types by hand.
    Returns a list: Palworld names aren't unique, and moderation must refuse
    an ambiguous match rather than hit whoever the API listed first."""
    return [p for p in players if p.get("name", "").lower() == name.lower()]


# ---------------- commands ----------------


def _countdown_seconds(args) -> int | None:
    """The `seconds` override this invocation asked for, or None for 'use the
    configured default'. `--now` is just `--in 0` with a friendlier name."""
    if args.now:
        return 0
    return args.in_seconds


def _countdown_running(client: DaemonClient) -> bool:
    """Is a countdown in flight that could still be cut short?"""
    cd = client.state().get("countdown")
    return bool(cd and cd.get("cancellable"))


def _resolve_target(client: DaemonClient, name: str) -> str:
    players = client.state().get("players", [])
    # An exact user ID passes straight through — it's the only unambiguous
    # handle when two players share a name.
    if any(p.get("user_id") == name for p in players):
        return name
    matches = find_players(players, name)
    if not matches:
        raise DaemonError(
            f"Can't find '{name}' online. (Kick/ban needs the player on the "
            "server to resolve their user ID — check `palctl players`.)"
        )
    if len(matches) > 1:
        listing = ", ".join(f"{p['name']} ({p['user_id']})" for p in matches)
        raise DaemonError(
            f"{len(matches)} online players are named '{name}': {listing}. "
            "Re-run with the exact user ID so the right one is hit."
        )
    return matches[0]["user_id"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="palctl",
        description="Drive the palctl daemon from a terminal. The daemon must be "
        "running (as your user) on this machine.",
    )
    from . import __version__

    p.add_argument("--version", action="version", version=f"palctl {__version__}")
    p.add_argument("--port", type=int, default=DAEMON_PORT, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="service state, FPS, players, memory")
    sub.add_parser("players", help="who's online")
    ev = sub.add_parser("events", help="recent daemon events")
    ev.add_argument("-n", type=int, default=20, help="how many (default 20)")

    sa = sub.add_parser(
        "save-audit",
        help="what's in your world folder, and how much of it is nobody's",
    )
    sa.add_argument(
        "--days", type=int, default=saveaudit.INACTIVE_DAYS,
        help=f"how long since a player's last session counts as inactive "
             f"(default {saveaudit.INACTIVE_DAYS})",
    )
    sp = sub.add_parser(
        "save-prune",
        help="remove departed players' records from Level.sav (dry run unless "
             "--apply)",
    )
    sp.add_argument(
        "--days", type=int, default=saveaudit.INACTIVE_DAYS,
        help=f"how long since a player's last session counts as inactive "
             f"(default {saveaudit.INACTIVE_DAYS})",
    )
    sp.add_argument(
        "--apply", action="store_true",
        help="actually rewrite Level.sav. Without this it only reports what it "
             "would remove. Requires a stopped server; takes and verifies a "
             "backup first.",
    )

    sa.add_argument(
        "--deep", action="store_true",
        help="also read Level.sav to count each player's characters and pals "
             "(slow, and needs memory proportional to the save)",
    )

    def _countdown_flags(parser: argparse.ArgumentParser, what: str) -> None:
        """`--in`/`--now` on anything that warns players first. Both are
        overrides for this one call; the default lives in Config so the usual
        case needs no flag at all."""
        g = parser.add_mutually_exclusive_group()
        g.add_argument(
            "--in", dest="in_seconds", type=int, metavar="SECONDS",
            help=f"seconds of in-game countdown before the {what} (0 = none)",
        )
        g.add_argument(
            "--now", action="store_true",
            help=f"no countdown — {what} immediately (or cut short one already "
                 "running)",
        )

    sub.add_parser("start", help="start the server")
    sub.add_parser("stop", help="save and stop the server")
    r = sub.add_parser("restart", help="restart with an in-game countdown")
    r.add_argument("--reason", default="Admin restart", help="shown to players")
    _countdown_flags(r, "restart")
    sub.add_parser("save", help="save the world now")

    sub.add_parser(
        "cancel", help="abort the restart/restore countdown that's running"
    )
    sub.add_parser(
        "skip", help="stop waiting out a countdown and run the operation now"
    )

    sub.add_parser("backup", help="take a backup now")
    sub.add_parser("backups", help="list backups")
    rs = sub.add_parser("restore", help="restore a backup (restarts the server)")
    rs.add_argument("name", help="backup name, as shown by `palctl backups`")
    _countdown_flags(rs, "restore")

    sub.add_parser("update", help="update the server via SteamCMD")

    an = sub.add_parser("announce", help="send an in-game announcement")
    an.add_argument("message", nargs="+")

    for verb in ("kick", "ban"):
        k = sub.add_parser(verb, help=f"{verb} a player by name (or exact user ID)")
        k.add_argument("name")
        k.add_argument("--reason", default=f"{verb.capitalize()}ed by admin")

    ub = sub.add_parser(
        "unban",
        help="unban a player by user ID (they're offline, so the ID — shown in "
        "the ban event and `palctl players` — is the only handle)",
    )
    ub.add_argument("user_id")

    sub.add_parser("ui", help="open the local web dashboard in a browser")

    args = p.parse_args(argv)
    client = DaemonClient(port=args.port)

    try:
        if args.cmd == "status":
            print(fmt_status(client.state()))
        elif args.cmd == "players":
            print(fmt_players(client.state().get("players", [])))
        elif args.cmd == "events":
            print(fmt_events(client.state().get("events", []), args.n))
        elif args.cmd == "save-audit":
            print(_save_audit(args.days, deep=args.deep))
        elif args.cmd == "save-prune":
            return _save_prune(args.days, apply=args.apply)
        elif args.cmd == "start":
            client.action("start")
            print("Server starting.")
        elif args.cmd == "stop":
            client.action("stop")
            print("Server saved and stopped. (`palctl start` brings it back; "
                  "crash auto-recovery won't fight an intentional stop.)")
        elif args.cmd == "restart":
            # `--now` on a countdown that's already running means "hurry it
            # up", not "start a second restart" — the daemon would answer 409
            # busy, which is technically right and useless to the person who
            # just wants the wait to end.
            if args.now and _countdown_running(client):
                client.action("skip-countdown")
                print("Cutting the countdown short — restarting now.")
            else:
                client.action(
                    "restart", reason=args.reason, seconds=_countdown_seconds(args)
                )
                print("Restart started — follow it with `palctl events`. "
                      "(`palctl cancel` aborts it, `palctl skip` skips the wait.)")
        elif args.cmd == "save":
            client.action("save")
            print("World saved.")
        elif args.cmd == "cancel":
            client.action("cancel-countdown")
            print("Cancelled — the server stays up.")
        elif args.cmd == "skip":
            client.action("skip-countdown")
            print("Skipping the rest of the countdown — going now.")
        elif args.cmd == "backup":
            client.action("backup")
            print("Backup started — it shows up in `palctl backups` when done.")
        elif args.cmd == "backups":
            print(fmt_backups(client.backups()))
        elif args.cmd == "restore":
            if args.now and _countdown_running(client):
                client.action("skip-countdown")
                print("Cutting the countdown short — restoring now.")
            else:
                client.action(
                    "restore", name=args.name, seconds=_countdown_seconds(args)
                )
                print(f"Restoring '{args.name}' — players are warned first, then "
                      "the server restarts. A safety copy of the current world is "
                      "taken too. (`palctl cancel` aborts it while it counts down.)")
        elif args.cmd == "update":
            client.action("update-server")
            print("Update started (backup → SteamCMD → restart) — follow it "
                  "with `palctl events`.")
        elif args.cmd == "announce":
            client.action("announce", message=" ".join(args.message))
            print("Announced.")
        elif args.cmd in ("kick", "ban"):
            user_id = _resolve_target(client, args.name)
            client.action(args.cmd, user_id=user_id, reason=args.reason)
            print(f"{args.cmd.capitalize()}ed {args.name}.")
        elif args.cmd == "unban":
            client.action("unban", user_id=args.user_id)
            print(f"Unbanned {args.user_id}.")
        elif args.cmd == "ui":
            # The token rides in the URL fragment: fragments never leave the
            # browser, and the page needs it to call the daemon's API.
            from . import netinfo
            from .config import Config

            token = localauth.get_or_create_token()
            host = Config.load().ui_bind_host
            open_url, lan_url = netinfo.dashboard_targets(
                host, args.port, token, netinfo.lan_ip()
            )
            print(f"Dashboard: {open_url}")
            if lan_url:
                # LAN access is on — this is the URL to open on another PC/phone.
                print(f"On this network: {lan_url}")
                print(
                    "  Open that on another device. The token in the link is the "
                    "only credential, so treat it like a password — and never "
                    f"port-forward port {args.port} to the internet."
                )
            elif not netinfo.is_loopback(host):
                # LAN bind requested, but we couldn't work out this box's address.
                print(
                    "  (LAN access is enabled, but palctl couldn't determine this "
                    "machine's network address — browse to http://<this-box-ip>:"
                    f"{args.port}/ and append #{token} from another device.)"
                )
            try:
                import webbrowser

                webbrowser.open(open_url)
            except Exception:
                pass  # headless box: the printed URL is the point
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
