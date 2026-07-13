# palctl

Palworld dedicated server control for Windows. **REST-native**, with a real
memory-leak watchdog and a **self-hosted Discord bot**.

> **Status:** v0.1. Works, but young. Issues and PRs welcome.

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
- Scheduled restarts with in-game countdown, autosave, rotating backups
- Join / leave / level-up events, synthesised by diffing the player list
- Session + playtime tracking in SQLite (Palworld remembers none of this)
- Server up/down detection

**GUI**
- Dashboard: FPS, frame time, memory sparkline, uptime, in-game day, base camps
- Players: level, ping, location, building count, kick/ban
- Console: announce (real spaces — REST, not RCON), save, backup, start/stop/restart
- **Settings editor**: parses the one-line `OptionSettings=(...)` blob into a
  searchable, grouped, typed form. Preserves unknown keys from future patches.
  Backs up the ini on every save, because SteamCMD `validate` wipes it.
- Config: paths, watchdog thresholds, schedules, Discord — all entered in the UI

**Discord bot**
`/status` `/players` `/playtime` `/announce` `/save` `/backup` `/restart` `/kick` `/ban`
plus join/leave, level-up, watchdog, and server up/down notifications.

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

**Requires:** Windows, Python 3.11+, a Palworld dedicated server, and RCON's
replacement turned on.

### 1. Enable the REST API

Palworld ships `Pal\Saved\Config\WindowsServer\PalWorldSettings.ini` **empty**.
That's normal. You must copy the contents of `DefaultPalWorldSettings.ini` (in the
server root) into it — editing the Default file itself does nothing.

*(palctl will detect the blank file and offer to seed it for you.)*

Then set, inside the `OptionSettings=(...)` line:

```
RESTAPIEnabled=True
RESTAPIPort=8212
AdminPassword="pick-something"
```

Restart the server.

> The REST API is **not** designed to be exposed to the internet — Pocketpair says
> so explicitly. `palctl` only ever talks to `127.0.0.1`. Don't port-forward 8212.

### 2. Run it

```
run-daemon.bat      creates a venv, installs deps, starts the daemon
run-gui.bat         opens the GUI
```

Open the GUI → **Config** tab → set your paths and admin password → Save.
Everything is entered in the UI. Secrets go into Windows Credential Manager
(DPAPI-encrypted), never into a config file.

### 3. Make the daemon permanent

```
nssm install palctl-daemon "C:\path\to\palctl\.venv\Scripts\python.exe" "-m" "palctl.daemon"
nssm set palctl-daemon AppDirectory "C:\path\to\palctl"
nssm start palctl-daemon
```

### 4. Discord (optional)

Create an app at discord.com/developers → Bot → copy the token → invite it to your
server with `applications.commands` and `bot` scopes. Paste the token into the
GUI's Config tab. Restart the daemon.

---

## License

AGPL-3.0. Use it, fork it, run it. If you host it as a service, your changes stay
open.
