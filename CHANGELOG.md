# Changelog

All notable changes to palctl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come
straight from git tags (setuptools-scm), so the tag *is* the version.

Installers for every release are on the
[Releases page](https://github.com/SteveWeed79/palctl/releases), each with a
`SHA256SUMS.txt` to verify the download.

## [Unreleased]

### Added
- **A frame-time watchdog — restart on the *slideshow*, not just the leak.**
  The memory watchdog restarts on RSS, but Palworld can bog down to single-digit
  server FPS while still under the memory limit. Opt-in in Config: when the
  server's own reported FPS stays below your floor for several consecutive
  polls, palctl restarts it — with the same courtesies as the memory watchdog
  (confirming samples, the shared cooldown, holding off while players are
  online, and ignoring FPS 0 readings from a booting server).
- **A second alert channel: one generic webhook.** If Discord is down or not
  set up — exactly when an unattended box has a problem — the daemon can now
  POST operational events (outages, watchdog restarts, backup failures, errors,
  updates) to any URL: an ntfy topic, a Discord/Slack incoming webhook, or your
  own endpoint. The payload carries the message as `content`/`text`/`message`
  so all the common receivers accept it unchanged. Join/leave chatter is never
  sent. Configure it in the GUI under Alerts.
- **Low-disk safety.** A full disk corrupts saves, kills the server, and breaks
  the backups you'd recover with. The daemon now warns (once per episode) when
  free space on the server or backup volume drops below a configurable floor,
  and a backup that wouldn't fit is skipped with a loud error instead of
  filling the volume mid-copy.
- **Graceful shutdown.** `systemctl stop` / service stop used to just kill the
  daemon mid-write. It now catches the stop signal, flushes the world (if the
  server is up), stops its loops, closes the API client and the database
  cleanly, and logs the shutdown — all bounded so it finishes well inside the
  service manager's timeout.
- **The daemon can now prove it's alive — and be caught when it isn't.**
  A public `/healthz` endpoint reports whether the poll loop is actually
  cycling (503 when stale), and under systemd `Type=notify` the daemon sends
  `READY=1`/`WATCHDOG=1` pings so `WatchdogSec` restarts a daemon whose event
  loop has wedged — the one failure a process supervisor can't see.
- **`GET /logs`** — a token-gated tail of the daemon's own rotating log over
  the control API, so a misbehaving daemon can be diagnosed from the dashboard
  machine without shelling into the box.
- **Restart every N hours, not just daily.** Many servers run a 6–8 h restart
  cadence to stay ahead of the leak; the schedule now supports it (Config →
  Schedule → "Or restart every"). 0 keeps the daily-at-a-time behaviour.
- **Probing the LAN-bound API is now visible.** When the dashboard is exposed
  on the LAN the token is the only credential, so rejected requests are now
  logged with the peer address (rate-limited so a misconfigured client can't
  flood the log).

- **A much more capable Discord bot — the real from-anywhere remote control.**
  Since the web dashboard is deliberately not internet-facing, the bot is how
  you run the server when you're away, so it grew the commands that were
  missing:
  - **`/start` and `/stop`** — the bot could restart but never start or stop the
    server. Both now go through the same desired-running intent the GUI/CLI use,
    so a Discord `/stop` is remembered and auto-recovery won't fight it.
  - **`/health`** — memory against the watchdog limit *with the leak forecast*
    (minutes until a restart is due on the current trend), plus CPU, FPS, and
    frame time; the embed turns red when memory is near the limit or a restart
    is close.
  - **`/leaderboard`** (top players by total playtime), **`/whois`** (a player
    card), **`/events`** (recent server events, like the CLI and dashboard have),
    **`/next`** (upcoming automatic restart/backup/update), and **`/help`** (a
    grouped command list).
  - **`/playtime` and `/whois` answer for offline players too**, resolved from the
    session history palctl already keeps — the common case is checking on someone
    who isn't on right now. Playtime now also counts the session in progress, not
    just finished ones. A player's live map position and platform ID stay
    admin-only, and are delivered to the requesting admin privately (an ephemeral
    reply) so an admin's lookup doesn't broadcast them to the whole channel.
  - **`/cancel`** aborts an in-progress restart countdown before the server
    actually goes down — change your mind after `/restart` and call it off.
  - **Autocomplete** for player-name and backup-name arguments (`/kick` `/ban`
    `/playtime` `/whois` `/restore`), drawing on the live player list, the session
    history, and the backups on disk — so you're not typing exact names on a phone.
  - The optional **live status embed** now carries the leak forecast, so a pinned
    message answers "is a restart coming?" at a glance. `/events` shows only
    non-sensitive event kinds to non-admins (no raw error/watchdog internals).
  - **Confirm/Cancel buttons** on the destructive commands (`/stop` `/update`
    `/restore`), gated to the admin who invoked them, so a mis-tap can't take the
    server down.

- **Reach the web dashboard from other devices on your LAN.** The daemon's
  dashboard/control API used to bind `127.0.0.1` unconditionally, so the
  dashboard answered only a browser on the server PC itself — opening it from
  another PC or a phone on the same network silently got nothing. A new
  **Config → Web dashboard → "Allow access from other devices on this network"**
  toggle (config key `ui_bind_host`, default `127.0.0.1`; `0.0.0.0` for LAN)
  opts into a LAN-reachable bind. `palctl ui` then also prints an
  `On this network:` URL to open on the other device, and the daemon logs a
  one-line warning at startup that the per-user token is the only credential
  once it's exposed. The safe default is unchanged — you opt in, and it takes
  effect on the next daemon restart. On Windows the daemon also opens the
  firewall for the dashboard port (private networks only) when LAN access is on
  and it's running elevated — otherwise binding to the LAN was a silent no-op,
  since the firewall drops the inbound connections — and closes it again when
  LAN access is turned off. Don't port-forward the port to the internet; for
  anything past a trusted LAN, an SSH tunnel or Tailscale still authenticates
  and encrypts the connection.

### Changed
- **The Linux unit is now `Type=notify` with `WatchdogSec`.** The daemon
  reports readiness to systemd and sends periodic liveness pings, so a wedged
  event loop (process alive, daemon not working) gets restarted automatically —
  re-run `palctl-daemon install-service` to pick up the new unit. The install
  paths also say so explicitly now when something unidentifiable is holding the
  daemon port, instead of silently spawning a daemon that loses the port fight.
- **The Windows service wrapper is now WinSW instead of NSSM.** NSSM's last
  release was 2014 and it is unmaintained; WinSW (the wrapper Jenkins ships) is
  maintained and configured *declaratively* — the whole service definition
  lives in one XML file that is rewritten on every install, so the bug class
  where a re-install inherited stale per-setting state (an old service account,
  old launch arguments) is structurally impossible. WinSW also grants the "Log
  on as a service" right itself when registering under a user account, removing
  one cause of Error 1069. The download stays SHA-256-pinned like NSSM's was;
  service removal and start/stop now go through plain `sc.exe`, which also
  means uninstalling no longer needs to download anything, and services
  registered by older NSSM-based palctl versions are migrated cleanly on the
  next install. The install CLI commands (`install-service`,
  `install-startup`) now exit nonzero on a verified failure, so scripts and CI
  can assert the outcome.

### Fixed
- **The WinSW wrapper cache is re-verified against the pinned SHA-256 on
  every use.** Before, whatever `winsw.exe` sat in the cache was trusted
  forever: a corrupted or replaced file would be registered to run as SYSTEM
  unchecked, and bumping the pinned WinSW version could never reach existing
  installs because the stale cached copy always won. A cached copy that fails
  the pin is now refetched from the pinned URL, and the cache write is atomic
  so an interrupted download can't leave a half-written binary behind.
- **A failed service registration can no longer masquerade as success.**
  WinSW's `install` exit code was ignored — setup could log "Service
  registered" for a service that doesn't exist — and on Linux every systemctl
  step was fire-and-forget, so a failed `enable` (unit will *not* start at
  boot, the exact thing you asked for) was invisible because the daemon still
  came up fine right now. Both now fail loudly with the tool's own error text,
  and the CLI turns them into a clean message + nonzero exit instead of a
  traceback — including "re-run with sudo" when installing or removing the
  systemd unit without root.
- **The memory-leak watchdog no longer goes blind when the server runs under a
  different Windows account than palctl.** In the common setup — PalServer as a
  LocalSystem service, the daemon under your login user — palctl couldn't read
  the real multi-GB `PalServer-Win64-Shipping.exe` and silently fell back to the
  ~7 MB bootstrap launcher: memory read near-zero, CPU read 0%, and the watchdog
  could *never* fire (this is what three rounds of "CPU reads 0%" fixes were
  really chasing). `find_process()` now follows the launcher to the real server
  it spawned, even when the server's name can't be read across the privilege
  boundary; and the daemon warns once — in the log and the event feed — when the
  server and daemon run under different accounts, naming the fix.
- **One clean install path: palctl *and* the game server under your user
  account ("Path A").** The "Run as a Windows service" option now registers both
  services under the invoking account (with a Windows-password field in the
  setup wizard), so they share one account — the watchdog can read the server,
  the Discord bot's DPAPI token stays readable, and both start at boot. This
  replaces the old lose-lose choice between a login-startup daemon that can't
  watch a SYSTEM server and a LocalSystem service that can't run the Discord bot.
  Setup now **refuses** — not merely warns — any combination that would land
  palctl and the server on different accounts (the classic default: login
  startup + a SYSTEM server service), so the watchdog-blinding split can't be
  installed in the first place.
- **A failed Windows service install now says *why* instead of a misleading
  catch-all.** `palctl-daemon install-service` used to let `sc.exe`/WinSW fail
  silently when not elevated, wait out a 30-second probe, and then blame the
  daemon ("registered, but not answering") for what was really a permissions
  problem — so a service that never registered looked like a broken daemon. It
  now checks for administrator rights up front and refuses fast with the fix
  (run elevated, or use `palctl-daemon install-startup`); reports a blocked or
  tampered WinSW download plainly instead of crashing with a traceback; and,
  when the service registered but the SCM won't start it, surfaces the actual
  reason — including **Error 1069** (a PIN-only/passwordless account can't host
  a service logon), read from the `WIN32_EXIT_CODE` the status parser used to
  discard. `uninstall-service` likewise no longer prints "removed" when a
  non-elevated `sc delete` was refused.
- **The diagnostics bundle now captures Windows service state.** A "daemon won't
  start" report is only diagnosable off-box if it shows *why*: the bundle now
  includes `sc query`/`sc qc` (the service's state, logon account, and binary
  path — no secrets) and any start-failure reason, so a service stuck on a logon
  failure or running under the wrong account is visible in the zip instead of
  looking like an unexplained down daemon. On Linux it captures `systemctl
  status`.
- **The settings editor gives fixed-choice options real pickers instead of a
  text box.** Palworld writes its enum settings as bare words, so the editor
  couldn't tell them from free text and showed `Difficulty`, `DeathPenalty`,
  `RandomizerType`, `LogFormatType`, and `AllowConnectPlatform` as blanks you
  had to type into — where `Nomal` or `itemandequipment` is a setting the game
  silently ignores. They're dropdowns now, restricted to the values the game
  accepts. `CrossplayPlatforms` (Steam/Xbox/PS5/Mac) became a row of checkboxes
  instead of a comma-string you had to spell exactly. A current value the editor
  doesn't recognise — a custom or future-patch token — is preserved as a
  selectable choice, so saving never quietly drops it. The settings with
  non-obvious behaviour now carry a hover helper (flagged with an ⓘ): what
  `Difficulty=None` vs a preset actually does, the four `DeathPenalty` levels,
  that `RESTAPIEnabled` must be on for palctl to work at all, that
  `AdminPassword` doubles as the REST API password, that `AllowConnectPlatform`
  is deprecated in favour of `CrossplayPlatforms`, and the permadeath pair
  (`bHardcore`/`bPalLost`). The editor's restart banner also now warns about the
  single biggest "my settings won't apply" trap — once a world exists the game
  reads most of these from that world's `WorldOption.sav`, not this file.
- **Two clicks, one restart.** Restart/backup/update/restore requests checked
  whether the server was busy and then started the operation as a background
  task — and the busy flag only flipped when that task actually began. Two
  near-simultaneous requests (a double-clicked button, GUI + Discord at once)
  could both pass the check, and the second operation would run right after the
  first — a surprise second restart. The server is now reserved synchronously
  in the same instant as the check; the second request gets the busy answer.
- **A watchdog restart that couldn't stop the server no longer reports
  success.** If even the force-kill ladder failed, the restart cycle went on to
  a no-op start and saw the *old, still-bloated* process answering — and called
  it recovered, resetting the watchdog for another 20-minute cooldown. It now
  reports the failure so the event feed says "needs a look" instead of lying.
- **A rotated admin password no longer triggers restart loops.** The REST API
  answering 401 (server up, wrong password) was treated like an outage, so
  crash auto-recovery would restart a perfectly healthy server — repeatedly,
  since a restart can't fix a password. It's now reported once as a
  configuration error and never drives recovery.
- **`/cancel` now actually skips the scheduled daily restart.** Cancelling the
  countdown used to only postpone it: the scheduler woke again, saw today's
  restart time still ahead, and immediately started a fresh countdown. A cancel
  now skips to tomorrow's slot.
- **Playtime survives a daemon restart.** Sessions left open by a previous
  daemon run were closed at zero length — so restarting the daemon while
  players were online discarded their whole in-progress session from
  `/playtime` and the leaderboard. They're now closed at the daemon's last
  recorded activity, keeping the playtime up to the restart.
- **The player differ no longer writes to the database on the daemon's event
  loop.** Join/leave session writes ran synchronously (and contended a lock
  with background writers), which could hitch polling and the control API on a
  slow disk — worst after a restart with a full server, which wrote one
  fsync'd insert per online player. Writes now run on worker threads, priming
  is one batched transaction, and the database runs in WAL mode.
- **Startup failures now reach the log file.** The likeliest one — the control
  port already taken by a leftover daemon — used to print to the stderr the
  service wrapper discards, leaving a silent restart-loop. It's now logged
  (and any unhandled startup error lands in the rotating log before exit).
- **The status API stopped serving stale data during an outage** (last-seen
  FPS/uptime next to a down server), and a config reload is refused while an
  operation is mid-flight instead of swapping settings out from under it.
- **The install verifies what it claims, instead of assuming.** "Installed and
  started" used to print no matter what the service manager actually did (every
  `systemctl`/`nssm` exit code was ignored), and login startup reported
  "running now" the instant the process was spawned. Now the daemon install
  only claims success once the daemon's own control port answers — on any
  platform — and otherwise says exactly where to look (`palctl-daemon run` in a
  console on Windows, `systemctl status`/`journalctl` on Linux). The re-install
  also sequences the Windows service manager properly: wait for the old service
  to actually stop before removing it, and for the removal to actually land
  before re-registering the name — a service left "pending deletion" (something
  holding a handle, e.g. an open services.msc) is now reported with its cause
  instead of being silently configured as a zombie.
- **Switching how palctl starts in the background now cleans up the old mode.**
  Re-running setup with a different background-startup choice used to leave the
  previous mechanism behind: picking the Windows service kept the login Run key
  (so the next login spawned a second daemon that fought the service over the
  control port), the fresh service couldn't bind that port while the old
  login-startup daemon still held it (NSSM restart-looped the new daemon while
  the old one kept serving), and unticking the background group entirely did
  nothing at all. Now the service install removes the Run key and clears the
  port before starting, login startup already replaces the service, and
  unticking removes both mechanisms and stops the running daemon — the same
  "unticking actually turns it off" contract the Discord toggle has. Setup also
  asks for admin rights when switching *away* from a registered service (the
  removal needs elevation, and used to fail silently without it), and the
  wizard now pre-selects "Windows service" when that's what is currently
  registered instead of silently defaulting back to login startup. On Linux, a
  stray non-service daemon (e.g. a dev checkout run by hand) is stopped before
  the systemd unit starts, instead of crash-looping it. The chosen mode —
  including "off" — is now persisted in the config, so a wizard re-run defaults
  to what you actually picked. `palctl-daemon install-startup` on the command
  line now also replaces any running daemon immediately (the Run key alone only
  takes effect at the next login), and when a leftover daemon service can't be
  removed because the prompt isn't elevated, it says so and prints the fix
  instead of pretending it worked.
- **Re-running the daemon install now actually restarts the daemon.** Installing
  the service over an already-running daemon wrote the new unit/exe/params but
  left the old process up: `systemctl start` no-ops on an active unit and
  `nssm start` no-ops on a running service, so the stale binary and settings kept
  running. Worse, on Windows an in-place re-install could inherit stale
  settings from the old registration — the `set` calls only overwrite what the
  new install specifies, so an old service account or old launch arguments
  survived. Install now rewrites the unit and restarts on Linux (`systemctl
  restart`), and on Windows stops, removes, and re-registers the service from
  scratch before starting it, so a reinstall is exactly what it says. The
  Windows login-startup path had the same gap — it skipped the launch whenever
  a daemon was already answering — and now replaces the running daemon instead,
  removing any leftover daemon *service* registration first so the service
  manager can't resurrect the old process (or double-start it at the next boot).
- **CPU in `palctl status` (and the dashboard/bot) is no longer stuck at 0%
  — for real this time.** `cpu_percent(interval=None)` measures the work a
  process did *between two calls on the same object*, so it returns `0.0` the
  first time and needs a steady stream of prior samples to mean anything. An
  earlier fix cached one `Process` and reused it, but that still read `0.0` on
  the first sample, whenever two callers (poll loop, `/state`, the bot) landed
  back-to-back, and any time the poll loop that primed it stopped running (e.g.
  the REST API was briefly unreachable — that poll returns early and never
  samples). `proc_stats()` now measures CPU over a real fixed window on every
  call instead of relying on cross-call priming, so a single isolated read
  (the bot's `/status`, a `palctl status` right after start) reports a real
  number the first time and every time. The sample runs off the event loop, so
  it doesn't stall the daemon. The value is still normalized to 0–100% of the
  whole machine instead of psutil's raw per-core sum, so an N-core box doesn't
  read e.g. "750%".
- **A Stop that doesn't actually stop is no longer reported as success.** The
  daemon's HTTP `/action/stop` (used by the web dashboard and the `palctl stop`
  CLI) discarded the result of the service stop and always answered `ok`, so a
  hung server that never confirmed STOPPED still showed "saved and stopped."
  Start/stop now go through the one shared implementation the Discord bot uses,
  and a stop that doesn't confirm surfaces as a failure (HTTP 502 with a message)
  everywhere — dashboard, CLI, and bot alike. The Stop intent is still recorded,
  so auto-recovery won't resurrect the server.

## [1.1.0] — 2026-07-17

### Added
- **The first-run wizard now covers backups and the Discord bot.** A **Backups**
  section (a backup folder, how often, and an optional off-site copy with a
  **Test** button) and an optional, opt-in **Discord bot** section (token,
  channel ID, admin role/user ID, with the same in-line help as the Config tab)
  are set up right in the wizard, so a first-run user discovers and enables them
  without hunting through the Config tab afterwards. The backup folder is
  pre-filled with one that actually exists — the built-in `D:\PalworldBackups`
  default silently failed scheduled backups (which run by default) on the common
  single-`C:`-drive box, because the folder's `mkdir` can't create a drive that
  isn't there.
- **Cloud / off-site backup mirror via rclone.** The backup mirror now accepts
  an [rclone](https://rclone.org) remote (`remote:path`, e.g.
  `gdrive:PalworldBackups`) in addition to a local path, so backups can be
  pushed off the box to Google Drive, Dropbox, S3, OneDrive, and anything else
  rclone speaks. palctl shells out to the rclone binary the user configured
  with `rclone config` — it never handles OAuth tokens or a cloud API itself.
  Uploads are idempotent, a mirror failure never fails the primary backup, and
  the daemon warns at startup if a remote is configured but rclone isn't
  installed.
  - **Retention only ever deletes palctl's own backups.** Pruning (local mirror
    *and* cloud) now lists and purges only directories matching palctl's own
    dated backup names, so a mirror pointed at a populated location — a shared
    network folder, another disk's root, or an rclone remote holding the user's
    other files — can never lose the user's unrelated data to retention. A cloud
    mirror must additionally point at a dedicated folder (`gdrive:PalworldBackups`,
    not the bare `gdrive:` root). Metadata calls (list/test/purge) are bounded by
    a timeout so a stalled remote can't hang the daemon or the Test button.
  - The **Config tab** now has a Backup mirror field with a **Test** button
    that verifies the target works (rclone auth + a dedicated folder for a
    remote, writability for a local path) before backups rely on it — run off
    the UI thread so it never freezes the window.
  - **Separate mirror retention**: the mirror can keep a different number of
    copies than the local disk (fewer off-site to save cloud cost, or more on
    cheap cold storage). New `Copies to keep (mirror)` setting; `0` = match the
    local `Backups to keep` count. Local retention is now editable in the GUI
    too.
- **The watchdog can now force-kill a server that ignores the stop.** A truly
  wedged `PalServer` — the classic memory-leak hang — can sit in `STOP_PENDING`
  forever, and every automatic recovery (memory watchdog, crash auto-recovery,
  scheduled/pre-emptive restart) was reduced to the same ineffective service
  stop, retried each cooldown. Those unattended restarts now escalate when the
  stop times out: `terminate()` the server process, then a hard `kill()` if it
  survives, then confirm the service reached STOPPED — with an event at each
  step so it's clear a hard kill happened (a world save is attempted first). The
  user's own **Stop** button is unchanged: it still reports an honest failure so
  a human can decide, rather than force-killing behind your back.
- **Releases now include version-stamped downloads.** Alongside the canonical
  `palctl-setup.exe` / `palctl-portable.zip` (unchanged, so winget and the docs
  still resolve them), each release also carries `palctl-setup-<version>.exe` and
  `palctl-portable-<version>.zip`, so a saved file's version is obvious from its
  name.

### Changed
- **Local backups always run, at least once a day.** Local backups are the
  safety net, so they're no longer something the UI can switch off or space out
  past daily: the backup interval is capped at 24h (the wizard, the Config tab,
  *and* the daemon all enforce it, so a stale or hand-edited config still honours
  the floor). The admin still chooses any more-frequent cadence.
- **Off-site backups are now an explicit on/off switch**, separate from the
  location. Turning off-site copies off keeps the configured target
  (`gdrive:PalworldBackups`, a `\\nas\` share, …) so it can be flipped back on
  later without re-typing it, instead of the old "clear the field to disable".
  Existing configs that had a mirror path set are treated as **on** across the
  upgrade, so nothing that was being copied off-site silently stops.

### Fixed
- **"Save config & reload daemon" now actually starts the Discord bot.**
  The daemon read the bot's enabled flag and token exactly once, at startup —
  so the natural flow (paste token, tick Enabled, hit Save) silently did
  nothing until the daemon was restarted, and the dialog's small-print
  restart warning was easy to miss. A config reload now relaunches the bot
  when it isn't running (never enabled, missing token, or a previously
  rejected token that's since been fixed). The one remaining restart case is
  swapping the token of a bot that is already connected, and the save dialog
  now says exactly that.
- **A broken system keyring no longer crash-loops the daemon.** On a box with a
  broken `cryptography` backend, reading the admin password made keyring's pyo3
  layer raise a `PanicException` — which derives from `BaseException`, so it
  slipped past the "reads must never crash the daemon" guard and killed the
  process before it even started, which under NSSM/systemd is a restart loop.
  Secret reads now survive it, fall back to the ini admin password, and log the
  `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` workaround. Saving a
  secret still surfaces the error, as before.
- **The daemon API answers malformed requests with a useful 4xx.** A control
  action missing its body field (`kick`/`ban`/`unban`/`announce`/`restore`) used
  to return `500 {"error": "'user_id'"}` — a bare `KeyError`; it now returns
  `400 {"error": "missing required field: user_id"}`, and a non-JSON or
  non-object body gets a clear 400 too. `/favicon.ico`, which browsers fetch on
  their own, is served instead of returning 401 and littering the console with a
  spurious auth error on every dashboard visit.
- **`SHA256SUMS.txt` on the Releases page now verifies on Linux/macOS.** It was
  written with CRLF line endings, which `sha256sum -c` / `shasum -c` reject with
  "no properly formatted checksum lines"; it's now LF, with lower-cased hashes
  in the exact `sha256sum` on-disk format.

### Security
- **The NSSM download is now pinned to a checksum.** `ensure_nssm` fetched
  `nssm-2.24.zip` from nssm.cc with no verification and then registered the
  unpacked binary as a LocalSystem service — so a compromised nssm.cc or a
  man-in-the-middle on that download was a path to SYSTEM-level code execution.
  The download is now verified against a hard-coded SHA-256 (NSSM 2.24 is
  immutable) and refused if it doesn't match. The Visual C++ redistributable,
  whose Microsoft `aka.ms` URL is evergreen and so can't be hash-pinned, now has
  its Authenticode signature checked before it runs: a positively tampered
  installer is refused, while a machine that simply can't verify still installs.
- **The release workflow pins its GitHub Actions by commit SHA.** The workflow
  that builds and attaches the shipped binaries runs with `contents: write` and
  used mutable tags (`@v4`, `@v2`); each is now pinned to a full commit SHA, so a
  retargeted tag can't slip new code into a release build.

## [1.0.1] — 2026-07-16

### Fixed
- **The installer (and frozen exes) show their icon everywhere in Explorer.**
  `app-icon.ico` was written with every frame PNG-compressed, but Windows only
  reads PNG icon frames at 256×256 — so any Explorer view that wanted a
  smaller frame (the Downloads folder, details view, small/medium icons) fell
  back to the generic-exe icon while 256px contexts looked fine. Frames below
  256 are now classic 32-bit BMPs, per the ICO spec.

## [1.0.0] — 2026-07-15

The first stable release. The 0.1.x line closed with a full
release-readiness audit — daemon lifecycle, data safety, API surface,
GUI/wizard, security, packaging, docs, and tests — with every confirmed
finding fixed, and the daemon, web dashboard, and CLI verified end-to-end
at runtime before tagging.

### Fixed
- **The web dashboard actually works when opened via `palctl ui`.** A
  variable named `history` shadowed the browser's `window.history`, so the
  token-stripping line threw before any script ran — exactly (and only) when
  the page was opened with a token in the URL, which is how `palctl ui` opens
  it. The dashboard rendered nothing but its header in every shipped release.
- **The Config and Settings tabs refresh after the setup wizard runs.** They
  used to keep pre-wizard values, so the natural first-day flow — wizard, then
  Config tab to paste a Discord token, Save — silently reverted the wizard's
  paths/port and wiped the freshly stored admin password.
- **Console buttons no longer freeze the window.** Stop/Start/etc. now run on
  a worker thread with a timeout matching the daemon's own service-wait, so a
  slow service stop can't lock the UI for 10 seconds and then claim the daemon
  was unreachable when the stop had actually succeeded.
- **Upgrades and uninstalls handle the default background mode.** The
  installer now stops a login-startup daemon before copying files (not just
  the Windows-service variant), restarts it afterwards, and the uninstaller
  kills a running daemon/GUI so no orphaned files or ghost daemon are left.
- **Headless Linux actually works as documented:** `install-service` now
  registers the systemd unit to run as the sudo'ing user, not root, so the
  daemon shares your `~/.config/palctl` and the `palctl` CLI can authenticate.
- **One crashed loop no longer kills the whole daemon** — a failure in e.g.
  the leak forecaster is logged and reported while polling, the watchdog, the
  scheduler, and the control API keep running.
- **Service control can't wedge the daemon:** sc.exe/systemctl calls are
  bounded by a timeout, run off the event loop, and a Linux box without
  systemd degrades to "UNKNOWN" instead of crashing the control API.
- **Down/up flapping is debounced.** One slow poll (six-second timeout —
  common under the very memory pressure palctl watches for) no longer
  announces a false outage, splits playtime records, and resets the leak
  forecaster's history.
- The forecaster's empty-server pre-emptive restart can no longer queue behind
  a watchdog restart and bounce the server twice back-to-back.
- **The Discord bot retries its first connection** (network not ready at boot
  used to kill it until the next daemon restart), and its messages are sent
  from a queue so a Discord rate limit can't stall polling or the watchdog.
- The setup wizard can no longer be dismissed with Esc / the title-bar X while
  setup is still running invisibly in the background.
- Kick/ban refuse an ambiguous player name (two players with the same name)
  instead of hitting whichever the API listed first; an exact user ID always
  works.
- The welcome message can't be used to ping @everyone via a player-chosen name.

### Added
- **`/unban`** — from the CLI (`palctl unban <user_id>`), the Discord bot, and
  the daemon API. Bans issued through palctl were previously irreversible
  in-app.
- **The web dashboard got a visual overhaul** — the GUI's app icon and action
  icons inlined (one brand across desktop and web), card layout on a page
  plane, a favicon, a watchdog meter under the Memory tile that shifts
  blue → amber → red as memory approaches the restart limit, time axis and a
  current-value dot on the sparkline, and a phone-width layout (the Tailscale
  / ssh-tunnel remote story is phone-first). Palette re-validated for both
  light and dark modes.
- **The web dashboard can act, not just watch.** `palctl ui` now has
  start/stop/restart, save, backup, update, announce, kick/ban, and
  restore-a-backup — the same daemon endpoints the GUI and CLI use, gated by
  the same per-user token. Destructive actions confirm in styled in-page
  dialogs (Cancel holds focus so Enter can't confirm by accident; Esc
  cancels); buttons grey out
  while an operation holds the server lock. It still binds 127.0.0.1 only:
  the README's new **"Manage it from your phone, safely"** section shows the
  ssh-tunnel and Tailscale patterns for full remote admin with zero ports
  exposed.

## [0.1.14] — 2026-07-15

### Changed
- **A failed pre-update backup now aborts the server update** (manual,
  scheduled, and Discord-triggered) instead of warning and updating anyway —
  updates are exactly when saves get corrupted, and without that backup a bad
  update can't be rolled back. A fresh install with no world yet still updates
  freely, and the old warn-and-continue behaviour is available by unticking
  **Update requires a backup** in Config.

### Fixed
- **Hot backups are now consistency-checked.** A backup taken while the server
  is running fingerprints the world before and after the copy; if the server
  wrote mid-copy (a potentially torn backup), the copy is retried in a quiet
  window. If no quiet window is found the backup is kept but flagged, with a
  warning suggesting a clean neighbour for restores.
- CI now installs `aiohttp` and `discord.py` for the test job, so the daemon's
  auth-token-gate and crash-auto-recovery tests actually run instead of being
  silently skipped on every push.

### Added
- `SECURITY.md` — how to report a vulnerability privately, and where palctl
  draws its security boundaries.
- This changelog, issue and pull-request templates.

## [0.1.13] — 2026-07-14

### Fixed
- **Data safety:** backups are crash-safe end to end (an interrupted copy can
  never be mistaken for a finished backup), restore and update verify the
  server actually stopped before touching the world, and the wizard's server
  install is guarded the same way.
- **Daemon resilience:** a manual stop is persisted across daemon restarts (the
  scheduler won't resurrect a server you stopped on purpose, even after a
  reboot), a corrupt `sessions.db` is quarantined instead of crash-looping the
  daemon, and background task failures are surfaced instead of vanishing.
- UI/UX audit follow-ups on top of 0.1.12's icon work.

## [0.1.12] — 2026-07-14

### Added
- **Icons across the whole app** — tabs, action buttons, a proper
  multi-resolution Windows app icon, and a **status-aware tray icon** (green
  running / amber stopped / red daemon unreachable). Follows the Windows
  light/dark theme live.

### Fixed
- **Safer restore** — malformed backup names that could, in an edge case,
  overwrite the world with the whole backups folder (or delete every backup)
  are rejected.
- **The scheduler respects a manual stop** — a server stopped for maintenance
  is no longer sprung back to life by the daily restart or auto-update.
- **Upgrades keep the background service running** — installing an update no
  longer leaves the daemon stopped until the next reboot.
- Truthful setup wizard (verifies the server really finished downloading;
  "Setup complete" only claims what ran), smarter port-conflict readiness
  check, atomic config/profile writes, duplicate-click guards on
  Backup/Restart/Update, more accurate leak forecasting after a daemon restart.

### Security
- Local API token file created with owner-only permissions; the web dashboard
  strips the token from the URL after loading; download steps (SteamCMD, NSSM,
  VC++) gained timeouts.

## [0.1.11] — 2026-07-14

### Added
- **Password-free background startup** — the wizard defaults to "start at
  login" via the per-user Run key, which needs no account password and ends the
  Error 1069 saga for PIN-only / Microsoft-account logins. A boot-time Windows
  service remains the option for headless boxes.
- `--version` on both `palctl-daemon` and the `palctl` CLI.

### Fixed
- The mouse wheel no longer silently changes settings while scrolling the
  settings form.
- The Config tab resizes and scrolls instead of forcing an oversized window.
- **Clean in-place upgrades** — re-running the installer upgrades the existing
  install (same folder, settings kept) instead of a second parallel copy, and
  stops the daemon first so the update can't fail on a locked file.

## [0.1.10] — 2026-07-14

### Fixed
- First-run robustness: daemon errors are surfaced in the GUI, running two
  server instances is flagged, and the wizard is reachable again.

## [0.1.9] — 2026-07-14

*(0.1.7 and 0.1.8 were never published.)*

### Fixed
- **The daemon service actually runs after a wizard install.** The wizard,
  running inside the GUI process, registered the *GUI* exe as the service — so
  nothing listened on the control port and every button returned
  `WinError 10061`. It now always registers the daemon exe.
- A stopped daemon reads as "Can't reach the palctl daemon — start it," not a
  raw socket error; the wizard has a real finish line; the REST-API error names
  its most common cause (editing `DefaultPalWorldSettings.ini` instead of the
  live ini).

### Added (since 0.1.5)
- **One operation lock** — watchdog, scheduled restart, SteamCMD update,
  restore, and crash recovery can no longer collide.
- Server updates back up the world first; optional **backup mirror** to a
  second disk or network share.
- **Leak forecasting** — predicts time-to-limit and (opt-in) restarts early
  while the server is empty.
- **`palctl` CLI** and a **local web dashboard** (`palctl ui`) for headless /
  ssh use.
- Metrics persisted to SQLite, so graphs survive daemon restarts.

## [0.1.0] – [0.1.6] — 2026-07-13/14

Initial public releases: the daemon/GUI split, memory-leak watchdog, scheduled
restarts and rotating backups, the settings editor, the Discord bot, the
first-run wizard, and the Windows installer — plus rapid packaging and
installer iteration. No per-release notes were published for these.

[Unreleased]: https://github.com/SteveWeed79/palctl/compare/1.0.0...HEAD
[1.0.0]: https://github.com/SteveWeed79/palctl/compare/0.1.14...1.0.0
[0.1.14]: https://github.com/SteveWeed79/palctl/compare/0.1.13...0.1.14
[0.1.13]: https://github.com/SteveWeed79/palctl/compare/0.1.12...0.1.13
[0.1.12]: https://github.com/SteveWeed79/palctl/compare/0.1.11...0.1.12
[0.1.11]: https://github.com/SteveWeed79/palctl/compare/0.1.10...0.1.11
[0.1.10]: https://github.com/SteveWeed79/palctl/compare/0.1.9...0.1.10
[0.1.9]: https://github.com/SteveWeed79/palctl/compare/0.1.6...0.1.9
[0.1.0]: https://github.com/SteveWeed79/palctl/releases/tag/0.1.0
[0.1.6]: https://github.com/SteveWeed79/palctl/compare/0.1.0...0.1.6
