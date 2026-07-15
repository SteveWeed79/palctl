# palctl

Palworld dedicated server control for Windows. **REST-native**, with a real
memory-leak watchdog and a **self-hosted Discord bot**.

> **Status:** released and in active development — installers for every version
> are on the [Releases page](https://github.com/SteveWeed79/pal-it-up/releases),
> with the history in [CHANGELOG.md](CHANGELOG.md). Issues and PRs welcome.

---

## Why this exists

There are already several good Palworld server managers. This one is different in
three specific ways:

**1. It uses the REST API, not RCON.**
RCON is deprecated. Pocketpair's recommended admin interface is the REST API, and
every other Windows GUI I could find still drives RCON. REST also gives us things
RCON simply cannot: server FPS, frame time, uptime, base-camp count, and
per-player level, ping, location, and building count.

**2. It restarts on the symptom, not the clock.**
Palworld's dedicated server leaks memory. The universal advice is "restart it on a
timer," which either kicks people for no reason or leaves the server a slideshow
for hours. `palctl` reads `PalServer-Win64-Shipping.exe`'s actual resident memory
from the OS and restarts when it's *actually* bloating — with a countdown, a world
save, and a hold-off while players are online (up to a hard limit, past which the
server is going to die anyway).

You cannot do this from a web panel or a cloud bridge. You have to be *on the box*.

**3. The Discord bot is yours.**
Your token, your machine, no subscription, no third-party bridge holding your
server's admin password.

---

## Two processes

```
palctl.daemon   headless, wrap in NSSM, always running
                → polls, diffs, watches memory, schedules, runs the Discord bot

palctl.gui      PySide6 window, open it when you want it
                → dashboard, players, console, settings editor
```

**Closing the GUI does not stop anything.** The daemon is what manages the server.
That split is deliberate: a GUI alone only helps when you're sitting at the server
PC, which is the situation most people are trying to get out of.

---

## What it does

**Daemon**
- Memory-leak watchdog (consecutive-sample confirmation, player hold-off, hard limit, cooldown)
- Leak **forecasting**: fits the actual memory growth curve and warns *before*
  the limit — and, opt-in, restarts early at a moment the server happens to be
  empty, instead of at the threshold later with players mid-session
- Scheduled restarts with in-game countdown, autosave, rotating backups —
  **consistency-checked** (a copy the server wrote through is retried, and
  flagged if it stays dirty) and optionally **mirrored** to a second disk, a
  network share, or an **rclone cloud remote** (Google Drive, Dropbox, S3,
  OneDrive …) for off-site copies — backups on the server's own disk don't
  survive the disk, and a house fire takes the network share with it
- Opt-in scheduled auto-update (Palworld patches constantly) — the same
  save → backup → SteamCMD → restart flow as a manual update, world backup
  included (updates are exactly when saves get eaten), and **no backup means
  no update** unless you opt out
- One **operation lock**: scheduled restarts, watchdog restarts, updates,
  restores, and crash recovery can't fire into the middle of each other
- Notifies when a newer server build is available, or a newer palctl release
- Opt-in crash/hang auto-recovery: if the API stops answering while palctl didn't
  stop the server, it brings it back — rate-limited so a crash-loop isn't hammered
- Join / leave / level-up events, synthesised by diffing the player list
- Session + playtime tracking in SQLite (Palworld remembers none of this)
- Metrics history in SQLite too, so the graphs survive a daemon restart
- Server up/down detection
- Rotating log file in `%APPDATA%/palctl/logs` (Palworld ships none)
- Localhost control API gated by a per-user token, so only you (not any local
  process) can drive start/stop/restore/kick/ban

**GUI**
- Dashboard: FPS, frame time, memory sparkline, uptime, in-game day, base camps
- Players: level, ping, location, building count, kick/ban
- Console: announce (real spaces — REST, not RCON), save, backup, **restore a
  backup** (with a pre-restore safety copy), start/stop/restart, and **update the
  server** (SteamCMD, with the ini guarded across `validate`)
- **Settings editor**: parses the one-line `OptionSettings=(...)` blob into a
  searchable, grouped, typed form. Preserves unknown keys from future patches.
  Backs up the ini on every save, because SteamCMD `validate` wipes it.
- Config: paths (with **Browse** and **Auto-detect**, and a live ✓/✗ that tells
  you the path is really a server before you save), watchdog thresholds,
  schedules, Discord — all entered in the UI — plus a one-click **Export
  diagnostics** (logs + config, no secrets) for bug reports
- **First-run wizard**: runs readiness checks (disk space, the Visual C++ runtime
  the server needs, admin rights, a free port), finds the server and steamcmd,
  turns on the REST API, can install the server from Steam for you, registers both
  Windows services, then **starts the server and confirms the REST API answers** —
  and prints the address your friends connect to (with the port-forward reminder)

**CLI** — `palctl`
```
palctl status | players | events | start | stop | restart | save
       backup | backups | restore NAME | update | announce MSG | kick NAME | ban NAME | ui
```
Talks to the daemon's token-gated localhost API, so it works anywhere the
daemon runs — ssh sessions, cron jobs, and the headless-Linux setup the GUI
can't serve. `palctl kick zoe` resolves the name to a user ID for you. The
installer ships it as `palctl.exe` (tick "Add palctl to the PATH" to use it
from any terminal); from source it's the `palctl` script pip installs.

**Web dashboard** — `palctl ui`
The daemon serves a read-only dashboard at `http://127.0.0.1:8830` (localhost
only, like everything else): live status, FPS, players, a memory sparkline with
the watchdog limit drawn in, and recent events. The page is static; the data
calls need your per-user token, which `palctl ui` puts in the URL fragment —
fragments never leave the browser.

**Discord bot**
`/status` `/players` `/playtime` `/announce` `/save` `/backup` `/backups` `/restore` `/restart` `/update` `/kick` `/ban`
plus join/leave, level-up, watchdog, server up/down, and update-available
notifications — with an optional auto-refreshing status message and a
`{name}` join welcome.

---

## What it does NOT do, and can't

**No chat relay.** Palworld exposes no chat-read endpoint and dedicated servers
ship no log file by default. If your Minecraft/Hytale bot mirrors chat into
Discord, this one can't — that's RE-UE4SS territory, not the supported API.

**No entity/base manager.** The `gamedata` endpoint is in Pocketpair's docs but
there is currently no way to enable it on a dedicated server — no INI setting, no
launch argument.

**No plugin framework.** Palworld's server is a closed UE5 binary. There is no
Torch equivalent and there can't be one without injection.

---

## Setup

The fast path is the installer + the first-run wizard. The manual path still
works if you'd rather drive it yourself.

### Option A — installer (recommended)

Download `palctl-setup.exe` from the
[latest release](https://github.com/SteveWeed79/pal-it-up/releases/latest)
(or build it yourself from `packaging/`, see
[packaging/README.md](packaging/README.md)). No Python needed. It installs both
binaries, adds shortcuts, and offers to register the palctl background service.
Then it opens the GUI, and the **first-run wizard** does the rest:

- **finds** your server root and steamcmd (registry, Steam libraries, or the
  running process) — nothing to type
- **installs the server for you** from Steam via SteamCMD, if it isn't already
- **enables the REST API** — seeds the blank `PalWorldSettings.ini`, sets
  `RESTAPIEnabled=True`, the port, and your admin password
- **registers the game server** as a Windows service and sets **palctl itself
  to run in the background** — password-free login startup by default, or a
  Windows service — so your server keeps being managed after a reboot or sign-in

You still need to point it at, or let it install, a Palworld **dedicated
server** — that software comes from Steam (app `2394010`). The wizard is happy to
fetch it; it just can't conjure it from nothing.

> The REST API is **not** designed to be exposed to the internet — Pocketpair
> says so explicitly. `palctl` only ever talks to `127.0.0.1`. Don't
> port-forward 8212.

> **The installer isn't code-signed.** palctl is free and hasn't bought a
> certificate, so Windows SmartScreen shows a one-time *"Windows protected your
> PC"* prompt — click **More info → Run anyway**. Every release ships a
> `SHA256SUMS.txt` so you can confirm the download matches what CI built. Removing
> the prompt for free is on the roadmap via SignPath Foundation's open-source
> code-signing program.

### Option B — from source

**Requires:** Windows, Python 3.11+.

```
run-daemon.bat      creates a venv, installs deps, starts the daemon
run-gui.bat         opens the GUI  (first launch pops the setup wizard)
```

The wizard handles detection, the REST API, an optional server install, and how
the daemon runs in the background. Prefer to do it by hand?

```
palctl-daemon.exe install-startup      # start at login — password-free (recommended)
palctl-daemon.exe install-service      # or run as a Windows service
```

**Login startup vs. service.** By default palctl starts with your login via the
current user's Run key. It needs **no password**, runs with full access to your
config and saved secrets (so the Discord bot works), and avoids Windows **Error
1069** — which a service under a passwordless / PIN-only account fails with. The
one tradeoff is it only runs while you're logged in; a **service** starts on boot
before login, so it's the better pick for a truly headless box. Secrets go into
Windows Credential Manager (DPAPI-encrypted), never a config file.

> **Which account runs the service?** By default the service runs as
> LocalSystem with `%APPDATA%` redirected to yours, so it shares your config,
> token, and logs — and it reads `AdminPassword` from the server's own ini,
> which is where Palworld keeps it anyway. The one thing LocalSystem can't
> reach is your DPAPI-encrypted secrets (the Discord bot token). Using the
> bot? Register with `palctl-daemon install-service --as-user` — the service
> then runs as *you* and sees everything the GUI saved. It asks for your
> Windows password once, passing it straight to the service manager.

### Linux (headless)

The daemon and its whole core — REST client, memory-leak watchdog, scheduler,
backups, path detection, and SteamCMD install/update — run on Linux too. Service
control uses **systemd** instead of NSSM, SteamCMD comes from the Linux tarball,
and paths resolve under `~/.steam` / `LinuxServer/`. Register the daemon with:

```
python -m palctl.daemon install-service   # writes a systemd unit, enables it
```

The desktop GUI/wizard are Windows-first; on a headless Linux host you drive
the daemon with the **`palctl` CLI**, the **web dashboard** (`palctl ui`
prints the tokened URL — open it in a local browser or over an ssh tunnel),
the Discord bot, and the service CLI.

### winget

Not yet — a manifest template lives in [packaging/winget/](packaging/winget/),
but it hasn't been submitted to
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs), so
`winget install SteveWeed79.palctl` won't find anything today. Until it lands,
grab the installer from the
[Releases page](https://github.com/SteveWeed79/pal-it-up/releases/latest).

### Discord (optional)

Create an app at discord.com/developers → Bot → copy the token → invite it to your
server with `applications.commands` and `bot` scopes. Paste the token into the
GUI's Config tab. Restart the daemon.

### Cloud / off-site backups (optional)

The **backup mirror** takes a second copy of every backup. Point it at a local
path (another disk or a `\\server\share`) for the simple case, or at an
[rclone](https://rclone.org) remote to push backups off the box entirely —
Google Drive, Dropbox, S3, OneDrive, and [dozens more](https://rclone.org/overview/).
palctl never touches OAuth tokens or a cloud API itself; rclone owns the auth
and the uploads.

1. Install rclone (`rclone.org/downloads`) and put it on `PATH`.
2. Run `rclone config` once to authorize your account — say you name the remote
   `gdrive`.
3. In the **Config** tab, set **Backup mirror** to `gdrive:PalworldBackups` (any
   `remote:path`) and hit **Test** to confirm palctl can reach it.

Each backup is uploaded under that path as its own dated folder. A mirror
failure never fails the primary backup — it's logged and the local copy is
untouched. If the mirror is a remote but rclone isn't installed, the daemon
warns at startup instead of failing silently.

The mirror keeps its own retention: **Copies to keep (mirror)** in the Config
tab can differ from the local **Backups to keep** — keep fewer off-site to save
cloud cost, or more on cheap cold storage. Leave it at `0` to match the local
count.

---

## Development

The platform-neutral core (ini parser, backups, session tracking, config,
scheduler, path detection, the SteamCMD argv/ini-guard, NSSM command building,
the REST-API bootstrap, the server-operation lock, the memory watchdog's
hold-off logic, the leak forecaster, and the CLI) is covered by tests that run
on any OS — only the daemon's service control, the actual downloads, and the
GUI need Windows.

```
pip install -e .[dev]
pytest
ruff check palctl tests
```

`pytest` runs in CI on Windows and Linux for Python 3.11 and 3.12; `ruff` runs
on Linux (Python 3.11). Keep both green.

---

## License

**AGPL-3.0-or-later.** Use it, fork it, run it. If you modify it and let others
use that modified version over a network, your changes stay open. The full text
is in [LICENSE](LICENSE).

**Commercial licensing.** If the AGPL doesn't fit — for example, you want to
bundle palctl into a closed-source product — a separate commercial license is
available. Open an issue or contact the maintainer.

**Contributing.** palctl uses a light CLA ([CLA.md](CLA.md)) so the dual-license
option above stays possible. See [CONTRIBUTING.md](CONTRIBUTING.md).

**Security.** Found a vulnerability? Please report it privately — see
[SECURITY.md](SECURITY.md).
