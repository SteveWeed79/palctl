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

from . import localauth
from .client import DAEMON_PORT, DaemonClient, DaemonError

# ---------------- formatting (pure, tested) ----------------


def _fmt_uptime(seconds: float) -> str:
    m, _ = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m:02d}m" if d else f"{h}h {m:02d}m"


def fmt_status(state: dict) -> str:
    lines = []
    alive = state.get("alive")
    api = "REST API answering" if alive else "REST API not answering"
    lines.append(f"server     {state.get('service', 'UNKNOWN')} ({api})")
    if state.get("operation"):
        lines.append(f"operation  {state['operation']} in progress")

    m = state.get("metrics")
    if m:
        lines.append(f"players    {m['current_players']}/{m['max_players']}")
        lines.append(f"fps        {m['server_fps']} ({m['server_frame_time']:.1f} ms frame time)")
        lines.append(f"uptime     {_fmt_uptime(m['uptime'])}")
        lines.append(f"in-game    day {m['days']}, {m['base_camps']} base camps")

    p = state.get("process")
    if p:
        lines.append(f"memory     {p['memory_mb']:,.0f} MB")
        lines.append(f"cpu        {p['cpu_percent']:.0f}%")
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
        f"{e['at'][:19]}  {e['kind']:<16} {e['message']}" for e in events[-n:]
    )


def find_player(players: list[dict], name: str) -> dict | None:
    """Resolve a player by in-game name (case-insensitive) — the daemon's
    kick/ban actions want the user_id, which nobody types by hand."""
    return next((p for p in players if p.get("name", "").lower() == name.lower()), None)


# ---------------- commands ----------------


def _resolve_target(client: DaemonClient, name: str) -> str:
    match = find_player(client.state().get("players", []), name)
    if match is None:
        raise DaemonError(
            f"Can't find '{name}' online. (Kick/ban needs the player on the "
            "server to resolve their user ID — check `palctl players`.)"
        )
    return match["user_id"]


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

    sub.add_parser("start", help="start the server")
    sub.add_parser("stop", help="save and stop the server")
    r = sub.add_parser("restart", help="restart with an in-game countdown")
    r.add_argument("--reason", default="Admin restart", help="shown to players")
    sub.add_parser("save", help="save the world now")

    sub.add_parser("backup", help="take a backup now")
    sub.add_parser("backups", help="list backups")
    rs = sub.add_parser("restore", help="restore a backup (restarts the server)")
    rs.add_argument("name", help="backup name, as shown by `palctl backups`")

    sub.add_parser("update", help="update the server via SteamCMD")

    an = sub.add_parser("announce", help="send an in-game announcement")
    an.add_argument("message", nargs="+")

    for verb in ("kick", "ban"):
        k = sub.add_parser(verb, help=f"{verb} a player by name")
        k.add_argument("name")
        k.add_argument("--reason", default=f"{verb.capitalize()}ed by admin")

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
        elif args.cmd == "start":
            client.action("start")
            print("Server starting.")
        elif args.cmd == "stop":
            client.action("stop")
            print("Server saved and stopped. (`palctl start` brings it back; "
                  "crash auto-recovery won't fight an intentional stop.)")
        elif args.cmd == "restart":
            client.action("restart", reason=args.reason)
            print("Restart with countdown started — follow it with `palctl events`.")
        elif args.cmd == "save":
            client.action("save")
            print("World saved.")
        elif args.cmd == "backup":
            client.action("backup")
            print("Backup started — it shows up in `palctl backups` when done.")
        elif args.cmd == "backups":
            print(fmt_backups(client.backups()))
        elif args.cmd == "restore":
            client.action("restore", name=args.name)
            print(f"Restoring '{args.name}' — the server will restart. A safety "
                  "copy of the current world is taken first.")
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
        elif args.cmd == "ui":
            # The token rides in the URL fragment: fragments never leave the
            # browser, and the page needs it to call the daemon's API.
            url = f"http://127.0.0.1:{args.port}/#{localauth.get_or_create_token()}"
            print(f"Dashboard: {url}")
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:
                pass  # headless box: the printed URL is the point
    except DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
