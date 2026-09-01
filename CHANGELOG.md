# Changelog

All notable changes to palctl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come
straight from git tags (setuptools-scm), so the tag *is* the version.

Installers for every release are on the
[Releases page](https://github.com/SteveWeed79/palctl/releases), each with a
`SHA256SUMS.txt` to verify the download.

## [Unreleased]

### Added
- **Auto-pause: an empty server puts itself away, and comes back when somebody
  tries to connect.** Off by default (`autopause_enabled`), and turning it off
  is always the safe direction — the server simply stays up.

  palctl **stops** the server rather than `SIGSTOP`-ing it, which is a
  deliberate departure from how the rest of the field does this. A suspended
  process still owns the UDP port, so the wake packet lands in its receive
  buffer where nothing can see it without root and packet capture; a stopped
  server releases the port and palctl just listens on it. And a suspended
  process still holds its leaked memory — palctl exists because Palworld leaks,
  and freezing a 12 GB process leaves 12 GB frozen. The cost is honest and
  belongs to the first player back: a stopped server takes tens of seconds to
  load.

  Every branch fails safe toward *running*. It will not act while an operation
  holds the lock, while the API is silent (that is the watchdog's business, and
  pausing would hide a sick server behind a deliberate-looking stop), when the
  player count is unknown as opposed to zero, or on a server somebody stopped
  on purpose — which it also never wakes. A freshly woken server gets a grace
  period so the player whose connection woke it isn't put away while still
  loading in.
- **`palctl save-prune --player NAME`** prunes one named player — the escape
  hatch the 90%-of-the-world refusal points at. It only ever *narrows* the
  automatic selection: the player must still be known, inactive and not
  excluded, so naming somebody is not a way around any rule.
- **The deep save audit now counts guilds and base camps**, and reports guilds
  whose every member has stopped playing. A guild with even one active player
  is that player's guild and is never counted — and a guild palctl could not
  read the membership of is never counted either, since an empty member set is
  a subset of everything and would make every unparseable guild a target.
- **The prune can be driven from the daemon**, under the same operation lock as
  a backup, with `apply` off by default over HTTP exactly as on the CLI.
- **A `/metrics` endpoint in Prometheus format.** palctl keeps seven days of
  metrics and could show about two hours of them, in its own sparkline, with no
  way to export any of it — so "was the server slow last Tuesday?" was
  unanswerable from inside palctl and trivial in Grafana. Deliberately the
  small version of the idea: a handful of well-labelled gauges collected at
  scrape time, built from the *same* document `/state` serves, because two
  collectors drift and a metrics endpoint that disagrees with the dashboard is
  worse than none. Token-gated like everything else. The most alert-worthy
  sample is `palctl_workers_degraded` — the daemon is up and not doing its job,
  which `/healthz` alone reports as healthy.
- **palctl can register the game server on Linux.** Setup's server registration
  was Windows-only and keyed off `PalServer.exe`, so on Linux it registered
  nothing and every later `systemctl start` went at a unit that did not exist —
  the supervision, the watchdog and the scheduler all silently had nothing to
  drive, on the platform the README advertises as supported. The unit is
  `Type=simple`, not `notify` (PalServer never calls `sd_notify`; under
  `Type=notify` systemd waits for a READY that never comes, then kills a
  healthy server), carries a working directory (the launcher resolves its
  engine relative to it), waits longer before restarting, and gets 120s to
  shut down so the world finishes writing. It is registered but **not
  enabled**: palctl's daemon owns the boot decision, and an enabled unit would
  start the server behind its back.
- **`palctl setup` — first-run setup without the desktop wizard.**
  `setup_flow.run_setup` was written Qt-free on purpose and then only ever
  called from the GUI, so a headless Linux install could not run setup at all.
  Dry run by default, like `save-prune`.
- **`palctl save-prune` removes departed players' records from `Level.sav`** —
  the fix for the save-size death spiral that palctl's memory watchdog could
  only treat the symptom of. It is the only thing in palctl that rewrites a
  world, and it is built as a series of refusals with a mutation at the end:

  * **Dry run unless you pass `--apply`.** You have to ask twice.
  * **The server must be stopped**, established by looking for its process
    rather than by asking the service manager. Rewriting a save the server has
    open corrupts it, and its next autosave would overwrite the result anyway.
  * **A backup is taken and *verified* first** (`backups.verify`) — an undo
    nobody checked is not an undo.
  * **Only players palctl can identify, has not seen for months, and has not
    already pruned.** A save file it cannot match to a session is never a
    target. A plan that would remove more than 90% of the world's records is
    refused outright, because that is far likelier to be an attribution bug
    than a world that is 90% abandoned.
  * **The rewritten save is re-read and checked before it replaces anything** —
    the targets gone, and every other player's record count unchanged. Only
    then is the file swapped, with the original kept beside it.
  * **An exclusion list beside the world** records who was pruned, so a player
    who comes back is never pruned a second time.
- **`palctl save-audit --deep` reads `Level.sav` itself**, and reports how many
  character records — a player's own character plus every pal they caught —
  are held for players who stopped playing months ago. That is the number the
  plain audit could only call a floor.

  It runs in a **separate short-lived process**, always. Parsing a
  multi-gigabyte save needs multiple gigabytes of RAM, and a daemon that does
  that in-process is one the OOM killer may choose — taking down the supervisor
  of a running game server to answer a question nobody was blocked on. Every
  failure of that child (crash, OOM kill, timeout, an upstream format change,
  output that isn't JSON) comes back as "couldn't read it" with a reason, and
  the file-size report stands on its own.

  This vendors [palworld-save-tools](https://github.com/cheahjs/palworld-save-tools)
  0.24.0 (MIT, no dependencies, 228 KB) under `palctl/vendor/`. Vendored rather
  than a pip extra because the Windows installer ships a frozen build with no
  Python and no pip — the users most likely to hit save bloat are the ones
  least able to `pip install` a fix. Nothing there is edited; palctl code
  reaches it only through `savescan.py`.
- **`palctl save-audit` — what's in your world folder, and how much of it is
  nobody's.** Palworld's `Level.sav` grows without bound: every player who ever
  joined leaves a character record, every guild a group record, every abandoned
  base camp its own, and none of it is ever collected. Past a few gigabytes
  saves stall and restarts outlast the players' patience. palctl now reports
  world and `Level.sav` size, and — using the session history no other tool in
  this space has — names the players who haven't been seen in months.

  It is diagnosis only: it reads sizes and filenames and does not parse,
  decompress or modify a single save. Two refusals are deliberate. A save file
  palctl can't match to a session is reported as **unknown, never as inactive**
  (a world restored from a backup is full of players palctl never met, and
  "I don't know" is not "nobody wants this"). And the reclaimable figure is
  stated as a **floor**, because it counts only the small per-player files —
  the weight is those players' records inside `Level.sav`, which needs a
  parser palctl does not yet ship.
- **Sessions now record each player's Palworld `playerId`.** Save files are
  named after that GUID, not the Steam user ID palctl was keyed on, so until
  now there was no way to look at `Players/<guid>.sav` and say whose it is —
  the prerequisite for any cleanup that deletes something. Added as a nullable
  column on the existing table; old rows keep a NULL, which is honest, and any
  future cleanup must treat unknown as do-not-touch.
- **The event feed now records who asked.** Four surfaces can stop this server
  — the desktop GUI, the web dashboard, the CLI and the Discord bot — and the
  feed recorded that a stop happened but never who wanted it, so the first
  question after a surprise had no answer inside palctl. Events now carry an
  actor and the surface it came from ("zoe (discord)"), shown by `palctl
  events` and carried on `/state`. Attribution is inherited by whatever an
  action spawns, so a restart whose countdown ends ten minutes later is still
  recorded as that person's restart, and it is absent for anything palctl
  decided itself — which is the answer to "did a person do this?".
- **Updates now warn the people they are about to disconnect.** An update takes
  the server down for longer than a restart does, and it was the one operation
  that did it with no notice — the countdown machinery existed and updates were
  simply left out of it. They now get the same countdown, the same in-game
  announcements, and the same two escape hatches (cancel, or skip the wait) as
  a scheduled restart, and collapse to a few seconds when nobody is online.
- **A Steam branch can be held.** `steam_branch` (and `steam_beta_password` for
  a branch that needs one) pass `-beta` through to SteamCMD, so a server can sit
  on a known-good build instead of taking whatever `public` is serving today.
  Palworld patches constantly and has shipped save-breaking builds; until now
  there was no way to say "not that one". Pinning to an exact depot manifest
  needs DepotDownloader and is not part of this.
- **Calendar-based backup retention.** `backup_keep_daily` / `_weekly` /
  `_monthly` keep the newest backup in each of the last N days, ISO weeks and
  months, layered on top of the flat count — a backup survives if any rule
  wants it, so turning this on can only ever keep more. Without it, a burst of
  manual backups (exactly what people take before doing something risky) evicts
  the entire older history, and the day you need a backup is usually the day
  you learn the corruption started last week.
- **Setup warns when backups land on the server's own disk.** That covers a bad
  update, a botched restore and a corrupt save — but not the disk, which is the
  failure the word "backup" makes people assume they are covered for. A
  warning, never a blocker.
- **Backups now record what they are, and can be checked without restoring
  them.** Every backup gets a `palctl-manifest.json`: the file list with sizes,
  whether the pre-backup save actually flushed, and whether the copy was
  consistent. `backups.verify()` reads it back — every file present at the size
  it was written at, plus a header check on each `.sav` that catches a
  truncated save without decompressing anything. Until now the only way to find
  out whether a backup was any good was to restore it and see, which is the one
  experiment nobody runs.
- **Off-site backups can be brought back down.** `rclone.pull()` fetches one
  backup from the remote into the local backup root, staged through a
  `.partial` and renamed, so an interrupted download never looks like a
  finished backup. Off-site copies exist for the case where the box is gone —
  palctl could upload, list and prune them, but not retrieve one, so the answer
  to a dead disk was still a manual rclone invocation worked out under pressure.
- **Setup now checks that something will actually start your server after a
  reboot.** palctl sets the game service to Manual only when its own daemon runs
  as a boot service and takes that job on; in every other arrangement Manual
  means nothing starts the server, on any reboot, forever — and it produces no
  error, because the daemon whose absence causes it isn't running to complain.
  The readiness checks (both the CLI setup flow and the GUI wizard) now compare
  the service's real start type against how palctl itself is set to start, and
  say plainly which of the two is supposed to be doing it. This is what catches
  anyone already left in that state by an uninstall from before the handback
  existed. A Disabled service is reported but never called a failure — that is
  somebody deliberately turning the server off.

### Fixed
- **A backup was taken even when the save before it failed, and filed as
  clean.** The pre-backup flush returns whether the server's REST API answered,
  and that answer was discarded — so when the API was wedged, which is exactly
  when a good backup matters most, palctl copied an unflushed world and
  reported success. The copy still happens (an older world beats no world) but
  it is now announced, and recorded in the backup's manifest so a restore can
  say the world may be older than the backup's timestamp.
- **Scheduled backups forgot where they were across a daemon restart.** The
  loop slept a full interval before its first backup, measured from daemon
  start, so a box that restarts more often than the interval never reached a
  backup at all — and nothing warned, because no backup had *failed*. The wait
  is now measured from the newest backup on disk, which cannot drift from the
  thing it describes. An overdue backup runs after a short grace rather than
  the instant the daemon comes up, since the server is usually still starting.
- **Nothing ever said "your backups aren't running".** Everything reported a
  backup that failed; the quieter case — scheduling switched off, or an
  interval of zero, while the operator believes backups are running because
  they set them up once — was silent. palctl now says so once per daemon run
  when the newest backup is older than it should be.
- **`sessions.db` was copied hot into every backup.** A live SQLite file copied
  byte-for-byte can land mid-transaction and restore as invalid, not merely
  stale, and nothing says so until someone opens it. The config snapshot now
  takes it through SQLite's own backup API, so what lands in the zip always
  opens.
- **A crashed worker loop was never restarted.** One escaped exception retired
  that loop for the life of the daemon — the memory watchdog, or the scheduler,
  simply gone — while the daemon stayed up, the control API kept answering and
  `/healthz` kept reporting "ok". Loops are now restarted with a 5s → 30s → 2m
  → 10m backoff and a bounded budget, and once that budget is spent the daemon
  reports itself `degraded` in `/healthz` and `/state` instead of claiming
  health it doesn't have.
- **The nightly auto-update took the server down whether or not there was an
  update.** The loop went straight to running SteamCMD, so on the majority of
  nights when Steam had shipped nothing it still stopped, updated and restarted
  the server, in front of whoever was playing. It now checks first, and fails
  *closed*: an inconclusive check is not evidence of a new build.
- **Discord notifications that failed to send said nothing at all.** The
  delivery path swallowed `DiscordException` with a bare `pass`, so the usual
  causes — the bot losing Send Messages on that channel, the channel being
  deleted — were permanent and invisible until someone noticed palctl had gone
  quiet. Failures are now logged.
- **The hung-daemon health task could never run in a source install.**
  `schtasks /Create` has no working-directory option — that lives in the task
  XML — so a scheduled task runs from the scheduler's own directory. For a
  frozen build that is harmless (an absolute path to `palctl-daemon.exe`, no
  arguments), but a pip or source install runs `python -m palctl.daemon`, which
  resolves `-m` against the current directory: the health check failed with "No
  module named palctl" every five minutes, forever. Nothing noticed, because
  `register_health_task` returned success — schtasks had registered the task
  perfectly well; it was the task that could not run. So those installs had no
  wedged-daemon recovery at all while being told they did. The command now
  carries its checkout directory when it needs one; the frozen path is
  unchanged.
- **palctl took over starting your server at boot, then could give that job up
  without handing it back.** Setup registers the PalServer service **Manual**
  when palctl itself runs as a boot service, on purpose: palctl's daemon then
  starts the server after a reboot, which is what lets a server you deliberately
  stopped stay stopped. That trade only holds while a boot-time daemon exists to
  keep its half — and nothing ever gave the job back. `palctl-daemon
  install-startup` (switching to login startup) left a daemon that does not
  exist until somebody signs in, so the server came up only if a human logged in
  within fifteen minutes of boot, and on a headless box never. `palctl-daemon
  uninstall-service`, and unticking background startup in setup, left PalServer
  Manual with nothing alive to start it at all — a server that silently never
  came back after any reboot, with no message anywhere and nothing in palctl
  that checked for it. Those paths now set the service back to Automatic and say
  so, so Windows resumes the job palctl is no longer doing. Only **Manual** is
  touched: Automatic already boots, and **Disabled is left alone** — that is
  somebody deliberately turning the server off, and quietly re-enabling it on
  the way out would be palctl making a decision that isn't its to make. When the
  change needs an administrator and palctl doesn't have one, it prints the exact
  `sc config` command rather than leaving the server silently unbootable. The
  stored WinSW config is updated to match, so a later setup re-run doesn't see a
  stale registration and bounce a live server over it. Windows-only; on Linux
  palctl never registers the game service.
- **A daemon that could never start was restarted forever, invisibly.** Both
  service wrappers were told to restart on failure and never told when to stop.
  Some startup failures are permanent — another daemon already holding the
  control port, an unwritable config directory, a half-finished upgrade — and
  the daemon would then be relaunched every five seconds for good. On Linux one
  restart per 5 s stays *under* systemd's own default rate limiter (5 starts per
  10 s), so the unit never reached `failed` and never appeared in
  `systemctl --failed`; on Windows, WinSW repeats its last `<onfailure>` entry
  indefinitely, so services.msc showed a service flickering rather than a
  stopped one. Either way the operator saw a service that looked like it was
  starting, a game server nobody was supervising, and no indication why. The
  systemd unit now carries `StartLimitIntervalSec=300` / `StartLimitBurst=5` (in
  `[Unit]`, where they have been since systemd 229 — under `[Service]` they are
  silently ignored), and the WinSW config escalates 5 s → 20 s → 60 s and then
  stops, with `<resetfailure>1 hour</resetfailure>` so clean uptime clears the
  count. A transient failure still recovers untouched; a permanent one stops and
  says so, and on Windows the five-minute health task keeps trying from there.
- **The CPU reading was being divided into invisibility before you saw it.**
  Twice before, this was reported as "CPU always shows 0%" and twice it was fixed
  in the *sampling* — and the sampling has been right since. The number was then
  normalised to a share of the whole machine and rendered with no decimals, and
  those two compound: the entire meaningful range for one busy core is 100/N on
  an N-core box. A Palworld server pegging a core showed **25%** on a 4-core box,
  **6%** on 16, **3%** on 32 and **2%** on 64; an idle-but-running server showed
  a flat **0%** on anything with 16 cores or more, next to a Memory tile and an
  FPS tile that were both live. So the reading looked broken on exactly the
  hardware people host on.
  palctl now reports **CPU-cores-equivalent** alongside the machine share —
  `1.00 cores (3.1%)` — because that is the figure that survives the box it was
  measured on, and because Palworld saturates one game-tick thread long before it
  runs out of cores, so "1.00 cores" is the number that says the server is at its
  ceiling. Same reading on the desktop GUI, the web dashboard, `palctl status`
  and Discord `/health`, from one shared formatter.
- **palctl could measure the wrong server entirely.** Candidate processes were
  collected into a dictionary keyed by process *name* — which they share — so
  with two Palworld servers running, each later one overwrote the earlier, and
  since psutil enumerates in ascending PID order the survivor was whichever
  server started **last**. That is precisely the leftover: a stale service
  registration firing at boot, or a second copy started on top of a server that
  has been up for days. Measured with a busy instance and an idle one, palctl
  picked the idle one five times out of five and reported its 0% CPU and few MB
  of memory as the server's — **and the memory-leak watchdog watched that
  instance too, so it could never fire.** The choice is now made properly: the
  install `server_root` points at wins outright (a second service is usually a
  second install, so this is exact rather than a guess), then resident memory,
  then the lower PID so repeated readings agree. `/state` also publishes how many
  server processes are running, so a surface can say so instead of quietly
  describing one of them.
- **The thin launcher's idle 0% was published as if it were the server's.** When
  palctl can only reach `PalServer.exe`/`PalServer.sh` and not the real binary
  behind it, it used to hand that process's numbers to every surface. It now says
  the reading is unavailable, which is the truth.

### Added
- **A CPU trend line in the desktop dashboard.** The CPU series was already
  sampled every poll, stored in SQLite and published on `/state` — and drawn by
  nothing, leaving the tile as a lone 0.3-second sample of a process whose work
  arrives in bursts. It now sits beside the FPS and Memory sparklines.

### Fixed
- **palctl blamed you for stops it made itself.** Three paths deliberately stop
  the server and then refuse to go on: an update or a restore that finds a
  PalServer process still holding the files, and a restore that fails with no
  world left in place. All three are right to leave the server down — but none
  of them recorded that palctl was the one who stopped it. The daemon reads
  "should be running" plus "the service says STOPPED and palctl isn't busy" as
  somebody stopping the server behind its back, so a few polls later it
  announced exactly that — *"The server was stopped outside palctl (services.msc,
  `sc stop`, or Task Manager)"* — about a stop it had performed seconds earlier,
  and quietly flipped the intent to "stay down" while the real reason scrolled
  past. Those aborts now record the stop as palctl's own and say so, so the
  supervisor stands down instead of accusing anyone, and the message you are
  left looking at is the one that names the problem.
- **A backup that worked could abort your server update.** Retention ran inside
  the backup's own `try`, so a prune that failed — one old backup directory held
  by an antivirus or the search indexer, which on Windows is routine and
  permanent — was reported as "Backup failed" for a backup sitting complete on
  disk. `update_requires_backup` (on by default) then read that as "no safety
  net" and aborted the update. One stuck folder was enough to stop a server ever
  updating again. Retention is now housekeeping that runs after the valuable
  work: it reports its own trouble, on its own terms, and never fails the backup
  it follows.
- **Retention gave up at the first directory it couldn't delete**, so every
  later run stopped in the same place and nothing was ever pruned again — while
  palctl warned about the low disk that caused. It now deletes everything
  deletable and reports once, naming what stuck.
- **The "your server runs from a different folder than palctl updates" warning
  could be silenced before it ever ran.** It fires once per daemon run, and the
  one shot was spent even when the check came back inconclusive — which it does
  whenever psutil can't read the server process's image path, exactly what a
  server running as SYSTEM under a login-user daemon produces. So the installs
  most likely to have a split root were the ones told least, and a wrong server
  root is *the* reason an update reports success and changes nothing. Only a
  conclusive reading counts now.
- **A watchdog restart that failed restarted the server again a minute later,
  forever.** The 20-minute cooldown was stamped only after a successful restart
  cycle, so if anything on that path raised — an event subscriber failing on the
  full disk that triggered the restart, a malformed `/metrics` reply while
  waiting for the server to come back — the next tick found memory still over
  the line and no cooldown, and went again. Every poll interval, kicking players
  each time. The cooldown now belongs to the attempt.
- **The dashboard's "update available" badge was wrong for hours after an
  update.** `update_status` is the standing answer to "is this server on the
  build Steam is serving clients?", and only the six-hourly check refreshed it —
  so the one moment it was guaranteed to be stale was straight after the update
  that fixed it. The update now records what it verified.
- **The pre-update copy of your settings was stored inside the folder SteamCMD
  rewrites.** PalWorldSettings.ini lives under the server install, so the one
  copy that makes a bad update undoable sat in the blast radius of the thing it
  protects — and if the update took the Config directory with it, restoring the
  ini raised, which aborted the repair before it re-asserted the REST API
  settings and left palctl blind to a server that was running perfectly well.
  The snapshot goes to palctl's own config directory (`ini-backups/`) now.
- **`.bak` copies of PalWorldSettings.ini piled up forever** in the server's own
  Config folder. Every path that rewrites the ini takes one first, and a single
  server update takes up to three; with scheduled auto-updates on, that is
  roughly a thousand files a year that nothing ever removed. The newest ten are
  kept.

### Changed
- Listing backups no longer measures every file of every backup when only the
  names are needed. Retention runs right after each backup and used to pay for a
  full recursive walk of the whole backup tree — a real cost with a couple of
  dozen retained multi-GB worlds on a slow disk or a network share.

## [1.2.7.0] — 2026-08-10

### Added
- **A restart or restore you can cancel — or hurry along — from anywhere.** The
  countdown was a hard-coded ten minutes with exactly one escape hatch, Discord's
  `/cancel`, which most installs don't have because the bot ships switched off.
  So an admin who wanted the restart *sooner* had no answer but to wait, and one
  who wanted it not to happen at all had none either — the dashboard even greys
  its buttons out for the duration, which is the app locking you out of the one
  decision you still wanted to make. Now every surface can do both:
  - **Cancel** calls the whole thing off; **now** stops waiting and runs it.
    They are different verbs with different outcomes, and both are on the web
    dashboard (a bar with the live clock and two buttons), the desktop Console,
    the CLI (`palctl cancel`, `palctl skip`), and Discord (`/cancel`, `/now`).
  - The countdown itself is published on `/state`, so every client shows the
    same clock instead of the countdown being visible only in the Discord
    channel it announced in.
  - **Its length is a setting** (`schedule.restart_countdown_seconds`, default
    600) and can be overridden per call: `palctl restart --in 60`,
    `palctl restart --now`, `/restart seconds:0`, or `{"seconds": N}` on the
    HTTP action.
- **Restores warn players first.** A restore used to drop everyone the instant
  the button was clicked — which is also why there was never a window in which
  to take back a mis-click. It now runs the same cancellable countdown
  (`schedule.restore_countdown_seconds`, default 60; 0 restores the old
  immediate behaviour).
- **A countdown with nobody to warn collapses to a few seconds.** Waiting ten
  minutes to announce a restart to an empty server is the single biggest source
  of "palctl made me wait for nothing". An unreachable REST API counts as empty
  for this, because an announcement reaches nobody there either. Off via
  `schedule.skip_countdown_when_empty`.

### Fixed
- **A failed restore could leave the server with no world — and then start it.**
  `restore()` copied the live world to the backup folder and deleted it *before*
  moving the restored copy into place, so any failure in that window (a full
  backup volume, a file the game still held open) left `SaveGames` gone. The
  caller then started the server, and Palworld generated a fresh world over the
  top of the problem. The swap is now two renames back to back, with the slow
  archive-the-old-world step moved *after* the live world is already correct; a
  failure there costs only the convenience copy and says so, instead of
  reporting a restore that succeeded as a failure. If the world is somehow still
  missing, the server is deliberately **not** started and the event says exactly
  where each copy is.
- **`palctl restore <typo>` reported success.** The daemon answered `200 OK` and
  spawned the restore, which then failed the name check and emitted an event
  nobody was watching. Unknown backup names are now a `400` with the reason,
  before anything is spawned.
- **Restore didn't check the install was actually free.** The update path has
  refused to touch a "STOPPED" service with a live PalServer process behind it
  since the version-mismatch bug; restore — which overwrites the world itself,
  and which that process is holding open — did not. Same guard, same wording.
- **"Nothing to cancel" no longer means "you were two seconds late".** Cancel
  and skip report three outcomes, not a bool, so an admin who just missed the
  window is told the operation is already under way rather than being told
  nothing was running, which reads as a broken button. The converse holds too:
  only a restart or a restore *has* a window, so cancelling during a backup, an
  update or the boot-time start says there is no countdown to interrupt (and
  names what is running) instead of "too late" — which would send someone
  looking for a clock that never existed.

## [1.2.6.0] — 2026-08-10

### Added
- **palctl now records why it is doing nothing.** The event feed says what
  happened; nothing said what palctl *decided* — and every hard-to-diagnose
  report in this project has been palctl reasoning correctly and silently
  (declining to recover a server it believed was meant to be stopped, holding
  off during an operation, throttling after too many restarts). Each decision is
  now kept with its reason and shown on `/state` (`why`, `decisions`) and a new
  `/decisions` endpoint. Repeats collapse, so a five-hour outage reads as one
  line with a count.
- **A standing "is this server on Steam's current build?"** on `/state`, instead
  of only a notification that scrolls away. A version mismatch is the failure
  players hit before the admin does — refused at the join screen while every
  palctl reading correctly says the server is healthy. "Couldn't tell" is
  reported as its own state, never as "up to date".
- **Drift detection for `PalWorldSettings.ini`.** palctl records what it wrote
  and reports anything else — a Steam update putting defaults back, a hand edit,
  a missing file — naming the settings that changed. The previous approach
  guessed at the damage ("is the file blank?") and missed the variant that
  actually happened. Losing the REST API settings is reported as urgent, because
  the symptom (palctl reporting a healthy server as down) points nowhere near
  that file. No values are stored, only hashes, so the snapshot can never leak
  the admin password.

### Fixed
- **An externally stopped server could still be restarted, for up to two
  seconds.** The decision "did somebody stop this?" was being made from the same
  short-lived cache that backs the dashboard's status display. Stale is harmless
  for *showing* a state and not for *deciding* one: with a short poll interval,
  two consecutive polls could be served the same 2-second-old "RUNNING" reading
  — long enough for palctl to conclude a server that had just been stopped by
  hand was a crash, and restart it. The recovery path now always reads the
  service manager fresh. Found by the new end-to-end scenarios, which is exactly
  the class of bug they exist for.

### Changed
- **The 'what to do about a server that isn't answering' decision is now one
  pure function** (`palctl/supervisor.py`) over one observation, instead of a
  sequence of guard clauses reading eight flags on the daemon. Both recent bugs
  in that area were ordering mistakes between those clauses. Same behaviour,
  stated once, with the whole policy pinned as a table.
- **End-to-end scenarios in CI.** `tests/sim` builds a fake machine — a Palworld
  server that can hang (accept the connection and never answer), a service
  manager that can be driven behind palctl's back or refuse to start, and a real
  daemon subprocess driven through its own HTTP API. Nothing is patched. Every
  failure this project has shipped was invisible to the unit suite and visible
  here in minutes.
- **The daemon's lifecycle CLI moved to its own module** (`palctl/daemoncli.py`):
  installing, starting, healing and removing the daemon is a program that runs
  once and exits, and it had been sharing a 2,200-line file with the one that
  runs for weeks. No API change — every name is still reachable as
  `daemon.install_service` and `python -m palctl.daemon` still runs the daemon,
  both pinned by tests.
- **A release whose CHANGELOG heading doesn't match its tag now fails**
  (`scripts/check_changelog.py`, run before anything builds).
  `docs/VERSIONING.md` has required that from the start; it drifted anyway,
  because nothing checked.

## [1.2.5.7] — 2026-08-09

Four audit passes over the reliability of palctl's own machinery, prompted by a
report of the app or the server "hanging up badly". Builds on 1.2.5.6, which
made updates prove the build landed; this pass is about everything else that
could stall, mislead, or quietly undo itself.

The through-line: palctl drives things that can stop responding — SteamCMD,
`sc.exe`/`systemctl`, `netsh`, `schtasks`, the Palworld REST API, its own worker
threads — and in a dozen places it waited on them forever, or drew the wrong
conclusion when they went quiet. Two could park the whole app indefinitely, one
made the daemon restart itself at the worst possible moment, one let a stranger
drive your server from Discord, and one turned a single comment in
`PalWorldSettings.ini` into a file palctl couldn't read.

Later passes stopped reading code and drove palctl from the outside instead —
against a server that hangs (accepting connections and never answering, which
is what a wedged PalServer actually does), and against a Steam update that
resets `PalWorldSettings.ini`. Those found the two failures most likely to read
as "palctl is broken": an update that leaves palctl permanently unable to see
a server that is running fine (top of **Fixed**), and a down server that palctl
reports once and then never mentions again because auto-recovery is off by
default (see **Changed**). Neither was reachable by reading files.

### Security
- **The Discord bot now only takes commands from your own server.** Slash
  commands are registered globally, so they appear in *every* guild the bot has
  been added to — and a Discord application is created with **Public Bot**
  switched **on**, so anyone holding the client ID (it's in the invite URL, and
  on the bot's profile) could add your bot to a guild of their own. There they
  hold Manage Server by definition, which is exactly what admin access falls
  back to when `admin_role_id` is unset — the default, and the setup the docs
  recommended. That was a working path to `/stop`, `/restore`, `/update` and
  `/ban` on a stranger's Palworld server. The bot now accepts commands from one
  guild only: `discord.guild_id` if set, otherwise whichever guild owns your
  notification channel; anything else is refused and logged. The check sits in
  front of the whole command tree rather than on each handler, so commands added
  later are covered automatically. If you have a channel configured there is
  nothing to do. The Discord guide now also says to turn Public Bot off.

- **A failed service registration no longer leaves your Windows account password
  on disk.** WinSW needs the password in its config XML for the single `install`
  command, after which the SCM holds it and palctl scrubs the file. The scrub
  wasn't in a `finally`, and `install` can raise rather than return non-zero (a
  quarantined or locked wrapper exe is an `OSError`) — on that path the password
  stayed in plaintext indefinitely, while setup reported failure, so nobody
  would think to look.

### Added
- **[Palworld server practices](docs/palworld-server-practices.md)** — the
  documented behaviour of Palworld and SteamCMD that this release's fixes come
  from, with sources, and where palctl agrees or deviates. Written so the
  deviations are a choice on the record rather than an accident, and so the next
  person doesn't re-derive it.

### Fixed
- **A stop palctl didn't make is now treated as a stop.** Auto-recovery decided
  from a single signal — "the REST API stopped answering" — which cannot tell a
  crash from an admin stopping the service. palctl only knew a stop was
  deliberate when *it* did the stopping, so stopping the server any other way
  (services.msc, `sc stop`, Task Manager) read as a crash and was undone within
  seconds. The server behaved as though it could not be turned off; the only way
  out was force-removing it. The service manager already knows the difference and
  palctl already reads it: a crash leaves the service RUNNING with nothing
  answering behind it, while a deliberate stop is the SCM reporting **STOPPED**.
  palctl now recognises that (confirmed over several polls, since its own
  restarts and the wrapper's `onfailure` both pass through STOPPED), records the
  stop as the intent it is — so the daily restart and auto-update don't bring it
  back later either — and says so. palctl's own Start undoes it.
- **…and a reboot no longer undoes it.** The other half of the same complaint:
  the game server was registered with the Windows startmode `Automatic`, so the
  service manager started it at every boot regardless of what palctl — or the
  admin — wanted. Stop the server on purpose, restart the machine, and it was
  back. Setup now registers PalServer as **Manual** whenever palctl itself runs
  as a boot service, and the daemon restores the recorded state at startup: a
  server that should be running is started, a server you stopped stays stopped.
  Nothing changes where palctl isn't there to do it — login startup, or no
  background palctl, keeps `Automatic`, because the SCM is then the only thing
  that can bring the server up. Restricted to actual boots, so the installer
  bouncing the daemon on upgrade (or the health task restarting it mid-outage)
  still can't start a server behind your back, and a server that *fails* to
  start at boot is reported as that rather than mistaken for someone stopping
  it. Existing installs pick this up the next time setup runs.
- **The account-split warning could be silenced permanently by one bad look.**
  If the Palworld server runs under a different Windows account than palctl,
  palctl can't read its memory — it lands on the idle launcher, and the leak
  watchdog can never fire, so the box climbs until it thrashes. A one-shot
  warning is the only protection against that, and its "already warned" flag was
  set *before* the check ran, unconditionally. A single poll where the process
  wasn't readable therefore suppressed the warning for the rest of the daemon's
  life — and an account split is itself a reason psutil can't read the process,
  so the servers that most needed telling were the ones told least. "Couldn't
  tell" and "no mismatch" are now distinct, and only a definitive answer latches.
- **`-pre-restore` safety copies are now bounded (newest 3) instead of never
  pruned.** They're full copies of the world, taken automatically on every
  restore, and being exempt from retention entirely reads as safe but isn't —
  a few restores leave several multi-GB worlds on the same disk as the live one,
  forever, and a full disk corrupts saves. They're counted separately from
  ordinary backups, so a restore's safety copy can never push a real backup out
  or be pushed out by one.

### Added
- **The first-run wizard now has a "Raids and the memory leak" section.** It's
  the only choice in setup that changes how the *game* plays rather than how
  palctl is installed, so it gets its own section and its own explanation:
  what the raid waves do to memory, how much difference turning them off
  makes, and why palctl leaves the decision to you. **Unticked by default** —
  leave it alone and Palworld behaves exactly as it ships. Ticking it is the
  only thing that writes `bEnableInvaderEnemy=False`; an admin who already
  turned raids off never has setup turn them back on.

### Changed
- **palctl now points at raids when memory is climbing.** Palworld's scripted
  invader waves spawn enemies the server never cleans up; operators
  consistently report RAM climbing at roughly half the rate with
  `bEnableInvaderEnemy=False`, making it the most-recommended companion to
  scheduled restarts. A memory-leak-focused tool had nowhere that said so. The
  settings editor now explains it, and the leak forecast — the moment you're
  actually thinking about memory — appends the suggestion, but only when raids
  are on and only when palctl could genuinely read the setting. palctl does not
  change it for you: raids are game content, and the evidence is operator
  experience rather than vendor guidance.
- **The settings editor now tells you when `WorldOption.sav` is silently
  overriding the file you're editing.** This is the single most common reason
  Palworld settings "don't apply", and it fails without any error:
  `PalWorldSettings.ini` is read when a world is *created*, after which the
  server copies the gameplay settings into `WorldOption.sav` and reads them from
  there — and a world imported from co-op or single-player always brings one
  along. palctl used to mention this in a sentence shown to everybody, which is
  noise on the servers it doesn't affect and easy to skim past on the ones where
  it discards every edit. It now looks for the file and, only when one exists,
  names the settings that will be ignored, the ones that still work (server
  name, ports, player cap, the REST API), and the path.
- **Updates no longer run `validate`, which is what was resetting your ini.**
  Every update path — the schedule, the GUI button, Discord `/update` — ran
  `app_update … validate`. `validate` is not an update: it is a full checksum of
  every file in the install against Steam's manifest, restoring anything that
  differs. Valve's guidance is to use it to repair a suspected-broken install,
  not to update one. palctl ran it routinely, which cost a full multi-GB
  verification pass every time and — as this code's own docstring admitted — is
  the thing that resets `PalWorldSettings.ini`. palctl was causing that damage
  itself, on a schedule, then trying to undo it afterwards. Plain `app_update`
  still installs the newest build. Validation is still available deliberately,
  as a repair: `POST /action/update-server {"validate": true}`.
- **A server update could leave palctl permanently blind, and silently wipe your
  settings.** This is the one that looks like "palctl is broken": after a
  SteamCMD update the dashboard shows nothing, the server can't be connected to,
  and restarting the service changes nothing — because nothing is wrong with the
  process. palctl took a backup of `PalWorldSettings.ini` before every update and
  then only ever restored it `if is_blank(...)` — only when the file came back
  *completely empty*. An update that leaves a **valid** ini full of Palworld's
  defaults sailed straight past that check, and those defaults carry
  `RESTAPIEnabled=False` and an empty `AdminPassword` — the exact two settings
  palctl needs to see the server at all. Every tuned rate, the server name and
  the player cap went with them. The backup was sitting on disk, unused and
  unmentioned. Now: a blank ini is restored wholesale as before, a *reset* ini
  has the admin's own values merged back over it (settings a genuine game patch
  added keep their new defaults; settings it removed stay removed), and the REST
  API settings are re-asserted after every update — the same idempotent call
  setup makes, which until now was never made again for the life of the install.
  palctl also says which settings it put back, and where the pre-update copy is.
- **A stalled SteamCMD no longer wedges palctl permanently.** This was the worst
  one. An update stops the game server *first*, then runs SteamCMD while holding
  the single server-operation lock. SteamCMD had no timeout — so a download that
  stalled, or a SteamCMD sitting on a login/Steam Guard prompt, left the server
  **down** and the lock **held forever**: the memory watchdog, crash
  auto-recovery, scheduled restarts, and every Start/Stop/Restart/Backup/Restore
  in the GUI, dashboard and Discord bot all answered *"busy: update is in
  progress"* until someone restarted the daemon by hand. `/healthz` still
  reported **ok** the whole time, because the poll loop was fine — so nothing
  monitoring the box would ever tell you. SteamCMD is now killed if it goes
  silent for 20 minutes (silence is the signal: it's continuously chatty while
  it actually works, and a total time cap would either kill legitimate multi-GB
  installs or be too loose to catch anything). The failure is reported as an
  event, the ini is restored, the server is started again, and the lock is
  released — the same path as any other failed update.
- **Killing a hung SteamCMD now kills its children too.** SteamCMD is a launcher
  (`steamcmd.sh` re-execs the real binary; the Windows build spawns helpers).
  Signalling only the process palctl launched leaves a child holding the
  inherited stdout pipe open, so the reader never sees EOF and the wait after
  the kill blocks for as long as that orphan lives — which would have recreated
  the exact hang above, from inside the recovery path. It also meant SteamCMD
  could still be writing into the install directory while the server restarted
  on top of it.
- **The daemon no longer goes deaf during startup on Windows.** The dashboard
  firewall sync (`netsh`) and the cloud-mirror check ran on the event loop,
  *after* the control API's port was bound. `netsh` against a stopped or sick
  Windows Firewall service can take tens of seconds, and during that window the
  daemon accepted TCP connections and answered **nothing** — the GUI showed
  "Can't reach the palctl daemon", the dashboard hung, and no port check could
  see anything wrong. Both now run off the loop and out of the startup path, so
  they delay only their own log line — not polling, not the control API, and not
  the READY signal the service manager waits on.
- **A wrong or missing `service_name` now fails in seconds instead of two
  minutes.** A service name the SCM/systemd doesn't recognise never reaches
  RUNNING or STOPPED, so every Start and Stop waited out the full 120-second
  timeout, held the server lock for all of it, and then failed anyway — with no
  hint as to why. palctl now recognises a sustained UNKNOWN as "the service
  manager has never heard of this name", gives up after about ten seconds, and
  logs the actual problem and how to fix it. A single blip (a query that timed
  out) is still tolerated.
- **Bounded every remaining external command.** `netsh` (firewall), `schtasks`
  (the health task), `systemctl` (the Linux unit), WinSW/`sc` (service
  install/start/stop) and the silent Visual C++ runtime install all ran with no
  timeout, so a wedged service manager or a competing installer could hang the
  setup wizard or the CLI with no output and no way out but killing the app.
  Each now has a timeout and reports a normal failure — these all already had a
  best-effort failure path; they just never reached it.
- **SteamCMD's build-id check is bounded too**, so a hung metadata query can't
  leak a process and silently stop the six-hourly update check from ever
  running again.
- **Daemon shutdown can no longer overrun the service manager's stop timeout.**
  Cancelling a task that is parked in a worker thread is a request, not a
  guarantee; teardown now waits a bounded time for those to unwind rather than
  blocking on the slowest external tool and being killed mid-teardown — which
  skipped closing the event store, the one step that writes to disk.
- **The daemon no longer restarts itself whenever the game server goes down.**
  `/healthz` is meant to answer "is this daemon alive", and the only thing that
  acts on it — the Windows health task — responds by restarting the daemon. But
  its liveness clock was stamped only on polls where the *Palworld REST API
  answered*, so a down game server was indistinguishable from a wedged daemon:
  60 seconds into any outage `/healthz` went 503, and ~15 minutes in the health
  task restarted a completely healthy daemon. That is the worst possible moment
  — auto-recovery is mid-flight, and the restart kills it, wipes the
  `crash_restart_max_per_hour` budget that exists to stop restart loops, and can
  leave the game server stopped between the stop and start of a restart cycle.
  The clock is now stamped on every completed poll cycle, whatever the outcome.
  Game-server reachability is still reported, as `alive`, and still acted on by
  the watchdog and auto-recovery — where it belongs.
- **An outage that spans a daemon restart is recoverable again.** Auto-recovery
  refuses to touch a server it has never seen up — a sound guard, but the flag
  lived only in memory, so a daemon that restarted *during* an outage came back
  believing the server had never worked and would never recover it. It stayed
  down until a human noticed. The flag is now persisted next to the Stop intent.
- **An empty `daemon_token` file no longer locks you out permanently.** A
  zero-byte token file made every caller mint a fresh random token that was
  never written, so the daemon and the GUI could never agree again — 401s that
  no restart fixes, reported by the GUI as a config-directory mismatch, which
  sends you chasing a completely unrelated fix. Getting there took nothing
  exotic: a crash or a full disk between creating the file and writing it, and
  the write failure was swallowed by design, leaving the empty file behind. An
  empty or whitespace-only token file is now treated as no token and replaced,
  and a failed write cleans up after itself instead of poisoning every later run.
- **Event history is pruned (30 days), like metrics already were.** Nothing ever
  read this table back, and it grew forever — inside `sessions.db`, which is
  snapshotted into *every* backup and mirrored off-site, so the bloat multiplied
  across every retained copy. The durable audit trail is the rotating file log,
  which every event is also written to.
- **A comment in `PalWorldSettings.ini` no longer hides the whole file.** The
  parser looked for the block's closing paren with a regex anchored to the end
  of the file, so a single trailing line — a comment, a note to self — made a
  perfectly good ini read as *"no OptionSettings block found"*. The settings
  editor then told you the file was blank and offered to re-seed it from the
  default, which would have destroyed the settings it had just failed to read.
  Quieter and worse: `read_admin_password` swallows that same parse error and
  returns empty, so the daemon couldn't authenticate to the REST API and
  reported the server as up-but-unauthorised — for one comment line.
- **A second `[Section]` in the ini is no longer silently corrupted.** The same
  greedy match reached past the block to the last `)` anywhere in the file, so
  another section landed *inside* the final option's value — and saving wrote
  both back mangled. The block's end is now found by matching parens (honouring
  quotes), and everything outside it — other sections, header comments, trailing
  notes — round-trips byte-for-byte. A truncated file now yields the options
  that survived instead of refusing outright.
- **Quitting the GUI no longer crashes it.** Qt aborts the process when a
  QThread is destroyed while still running, the tray's Quit went straight to
  `QApplication.quit`, and the state poller loops forever by design — so it was
  *always* running at quit time. Every exit ended in an abort (a "palctl-gui.exe
  has stopped working" dialog on Windows), which is the last thing a user sees.
  Worker threads now register themselves and are drained on `aboutToQuit`;
  anything still busy after a short grace — an `/action/stop` can legitimately
  be in a long call — is terminated rather than allowed to abort the app. The
  poller also sleeps in slices so quit is immediate instead of waiting out the
  poll interval.
- **Settings-editor group headings show their ampersand.** Qt reads `&` in a
  title as a mnemonic marker, so "Difficulty & rates" rendered as
  "Difficulty _rates" (and likewise "Base & building", "Pals & eggs").
- **A corrupt `config.json` can't crash-loop the daemon after all.** The
  recovery path renames the bad file aside and carries on with defaults — but
  the rename itself can fail (a Windows AV scanner or the search indexer holding
  the file open is a `PermissionError`), and that escaped, killing the daemon
  *before* `asyncio.run`, in the code written to prevent exactly that crash
  loop. The quarantine is now best-effort, as it always claimed to be.

### Changed
- **A down server no longer fails silently when auto-recovery is off.** Watching
  a real hang from outside, the entire output was one *"🔴 Server is down."* and
  then nothing, ever — which is indistinguishable from palctl itself being
  broken, and is the likeliest reason anyone concludes that it is. The recovery
  that would have fixed it (`watchdog.auto_restart_on_crash`) is **off by
  default**, deliberately: restarting your server unasked shouldn't be a
  default. But nothing said so. palctl now reports, once per outage, that the
  server is down and it has been told not to restart it — naming the setting to
  change. Turned on, behaviour is unchanged.
- The SteamCMD stall timeout is 20 minutes rather than 10. The two errors aren't
  symmetric: a real wedge is permanent, so noticing ten minutes later costs ten
  minutes once, while killing a healthy run costs a failed install — and
  SteamCMD's commit phase after a large depot download is legitimately quiet for
  minutes on a slow disk.
- CI now runs the GUI's thread-lifecycle tests in the import-smoke job, which
  already has a headless Qt. They need a real `QApplication`, so nothing else
  could have caught the crash-on-quit above.

## [1.2.5.6] — 2026-07-29

A server-update honesty pass. Every fix here targets the same end state: the
server left running an old build while palctl reports success, which players
only ever meet as **"version mismatch"** on the join screen.

### Fixed
- **The update check no longer goes silently blind on Steam-client installs.**
  palctl read the installed build id from `<server root>/steamapps/
  appmanifest_2394010.acf` — correct for its own SteamCMD installs
  (`+force_install_dir`), but *wrong* for anyone who installed the dedicated
  server through the Steam client, where the game sits in
  `<library>/steamapps/common/PalServer` and the manifest lives two levels up.
  Those installs read as "build unknown", and because the check needs both ids
  to compare, it quietly answered "no updates" forever — so the first sign of a
  patch was players being refused. Both layouts are now read, and a build id
  that genuinely can't be found says so once instead of passing for "you're up
  to date".
- **An update that didn't actually apply is no longer announced as a success.**
  SteamCMD's exit code was taken as proof, and it isn't: it exits 0 for
  "nothing to do", and a blocked overwrite (a locked file, a full disk, a
  `force_install_dir` that landed somewhere else) can still finish tidily.
  palctl now compares the build id on disk with Steam's latest *after* the
  update and, when they don't match, says the update did not land and lists the
  usual causes — instead of restarting the server onto its old binaries and
  reporting "✅ back up".
- **Updates abort when something still holds the install open.** A hung
  shutdown or a leftover second server service can leave `PalServer` running
  after the service manager reports STOPPED. SteamCMD cannot replace files a
  live process holds and, on Windows, fails that overwrite quietly — the old
  binaries survive the "successful" update. palctl now checks for a server
  process running out of the install directory before rewriting it, and aborts
  with the PID and the fix rather than half-applying a patch. (A process whose
  path can't be read — the server-as-SYSTEM split — is never blamed; the
  post-update build check covers that case instead.)
- **palctl now warns when it is updating an install nobody runs.** `Server root`
  (what SteamCMD rewrites) and `Service name` (what palctl starts) are separate
  settings, so they can point at two different copies of the server — the usual
  result of a second install, or of adopting palctl onto a box that already had
  a service. Every update then lands on the unused copy: the console says
  success, the live server never changes build, and no amount of re-updating
  fixes the version mismatch. The daemon now compares the running server's own
  image path against the configured root and reports the discrepancy once at
  startup, naming both paths.
- **The update-available notice explains what's at stake.** It now says that
  players whose game client has already updated will be refused with a version
  mismatch until the server is on the same build, and update events carry the
  build id they moved from and to (`Build 100 → 200`) so the event feed shows
  what actually changed.

## [1.2.5.5] — 2026-07-19

An install-, daemon-, and desktop-GUI reliability pass, done after a full
codebase audit — the setup/uninstall/service lifecycle and the GUI hardened,
with the accumulated 1.2-line work now filed under a version heading.

### Added
- **A hung daemon on Windows now heals itself.** The service wrapper restarts
  a *crashed* daemon, and systemd's watchdog restarts a *wedged* one on Linux —
  but on Windows a daemon that was alive-yet-stuck (the state `/healthz`
  reports with a 503) was only ever *visible*, never acted on. Registering the
  daemon (service or login startup) now also schedules a Task Scheduler job
  that runs `palctl-daemon health-check` every 5 minutes: it probes `/healthz`,
  counts consecutive failures across runs, and after three (~15 minutes of
  confirmed wedge) restarts the daemon the way it's actually deployed — then
  verifies the control port answers, never assumes. One blip (a restart in
  progress, a box waking from sleep) triggers nothing, and a single healthy
  probe resets the streak. The task is removed with whatever registered it, so
  it can never resurrect a daemon you turned off.
- **Every backup now carries palctl's own settings — disaster recovery for the
  brain, not just the world.** Each backup folder gains a `palctl-config.zip`
  with `config.json`, the daemon state, and the playtime/session history, so a
  dead disk no longer costs your whole setup alongside nothing (the world was
  covered; the config that manages it wasn't). It rides inside the backup
  directory, so retention and the off-site mirror cover it with zero new
  machinery; restores explicitly exclude it, so it can never leak into
  SaveGames. Deliberately whitelisted: the API token (a local secret) and the
  logs never leave the box.
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
- **Packaging docs and installer comments brought in line with reality, and the
  uninstall scope written down.** The packaging README still described an
  installer that downloaded NSSM and registered a service at install time —
  neither of which it does any more (WinSW and the VC++ runtime ship inside the
  build, verified, and no service is registered by the installer). Stale
  comments that called login-startup "the wizard's default" were corrected, and
  what uninstall deliberately leaves in place (the separate PalServer game
  service and the config directory) is now documented in `install-design.md` and
  the installer script rather than surprising anyone.
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
- **The desktop app now runs a single instance per user.** Closing the window
  only hides it to the tray (the daemon is a separate process, so the server
  keeps being managed), which meant every Start-Menu / desktop / tray click and
  the installer's "Launch palctl" spawned *another* `palctl-gui.exe` that kept
  running invisibly — a box could end up with four stacked on the installer's
  "these applications should be closed" screen, which also made upgrades flaky.
  A second launch now surfaces the running window instead of stacking a process.
- **Re-running setup no longer restarts a healthy server.** The wizard
  re-registered the Palworld service unconditionally, and re-registration is a
  stop→restart — so opening setup just to change a backup folder or add the
  Discord bot would bounce a live server and disconnect its players. Setup now
  skips re-registration when the service is already registered with exactly the
  configuration it would write, and only ever replaces (and restarts) on a real
  change.
- **Discord `/restart` and `/update` report "busy" instead of silently
  queueing.** They now match the dashboard's behaviour: if the server is
  mid-operation, the bot says so rather than stacking a second countdown or
  update behind the first.
- **Saving Config no longer rewrites — or accidentally blanks — your saved
  secrets.** The admin password and Discord token were written to the keyring on
  every Save, and because the fields are prefilled, clearing one and saving an
  unrelated setting could wipe a working REST password or bot token. Save now
  writes a secret only when it actually changed (unticking Discord, not clearing
  the token, is still how you turn the bot off).
- **Setup refuses to register a nameless service account.** If Windows reported
  no account name for the process, `--as-user` registration used to fall through
  to LocalSystem — silently re-creating the account split it exists to prevent.
  It now fails with the cause instead.
- **The wizard's "Windows password needed" message no longer points at a button
  that was removed.** It now names the real choices, including the console
  escape hatch (`palctl-daemon install-startup`) for PIN-only / passwordless
  accounts that cannot host a service logon.
- **The GUI's "daemon rejected the token" 401 under a user-account service —
  the service now really shares your config.** Windows builds a service's
  environment from the SYSTEM block: `%APPDATA%` is set by your interactive
  shell at login, not by the service manager — even when the service logs on
  as your own account. So an `--as-user` daemon silently read
  `<profile>\.config\palctl` while the GUI read
  `<profile>\AppData\Roaming\palctl`: two config folders, two tokens, and
  every GUI call answered 401 despite both running as the same user (latent
  since the wrapper swap; the moment `--as-user` became the recommended path,
  it bit). The service definition now injects the `APPDATA` redirect for
  user-account services too — as it always did for LocalSystem — and the 401
  message explains the actual cause and fix instead of suggesting a
  re-registration that wouldn't have helped. And the fallback itself is
  fixed at the root: with `%APPDATA%` absent, palctl on Windows now falls
  back to `<profile>\AppData\Roaming` — where the GUI actually lives —
  instead of a Linux-style `~/.config`. That means **updating palctl alone
  heals an affected install**: even under an old service registration with no
  redirect, the daemon now computes the same folder as the GUI. No re-running
  the wizard, no re-registration, no hand-edits.
- **The wizard's background section is one switch, not a menu.** The
  login-startup option is removed from the wizard entirely — not disabled, not
  hidden: every alternative to "service under your account" either couldn't
  watch the server or couldn't run the Discord bot, and offering it was how
  users ended up in the broken split. The group is now a single checkable
  choice: background on (service under your account, password field right
  there) or off. A legacy "login" choice from an older config maps to service
  on the next wizard run. PIN-only accounts that genuinely can't host a
  service logon still have `palctl-daemon install-startup` from a console — a
  power-user escape hatch, no longer a wizard option.
- **Audit of the NSSM→WinSW conversion — four gaps closed.** The wrapper swap
  (1.2.3) kept NSSM's runtime-download pattern and picked up WinSW's config
  model without re-examining what NSSM had been providing implicitly:
  - *A service-account password no longer outlives its one moment of use.*
    WinSW takes the account password via its XML config file, and palctl left
    it there — a Windows account password in a plaintext file for the lifetime
    of the service (NSSM passed it straight to Windows, which stores it
    encrypted, and kept nothing). The password is now scrubbed from the XML
    immediately after registration; the service keeps working (Windows itself
    holds the credential from that point).
  - *The cached wrapper binary is verified on every use, not just at download.*
    Anything sitting in palctl's cache becomes a SYSTEM service binary; a
    tampered copy is now discarded and replaced through the verified paths. A
    manually dropped copy that matches the pin still works.
  - *The game service gets 90 seconds to stop, not WinSW's 30.* PalServer
    flushes the world on the way down; on a plain `net stop` or system
    shutdown the wrapper would have killed it at 30s — a world-corruption risk
    NSSM's escalation ladder never had this sharply. 90s matches how long
    palctl itself waits for a stopping server.
  - *A game service the SCM refuses to start is reported with the actual
    reason* (Error 1069 & co., read from the service's recorded exit code)
    instead of setup waiting four minutes for a server that never launched and
    then blaming the REST API.
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
- **Setup no longer dies on `CERTIFICATE_VERIFY_FAILED` — and no longer dies
  halfway.** On a machine where Python can't verify HTTPS against the system
  certificate store (an antivirus doing HTTPS scanning, a broken store), the
  WinSW / VC++ / SteamCMD downloads failed with a bare `_ssl.c:1010` — after
  setup had already saved the config and edited the server ini. Downloads now
  retry verification against the CA bundle `certifi` ships (verification is
  never disabled — both failing still fails closed, with a message that names
  the antivirus/proxy cause), the WinSW failure spells out the manual escape
  hatch (download the exact release asset in a browser, drop it in palctl's
  `bin` folder, re-run setup — with the SHA-256 to check it against), and setup
  fetches everything it needs to download *before* touching a single byte of
  config, so a blocked download aborts a setup that changed nothing.
- **The installer now ships WinSW inside the build — no install-time download
  at all.** The release build downloads the service wrapper once, verifies it
  against the pin, and places it next to palctl's exes; `ensure_winsw` prefers
  that bundled copy (hash-checked again at use; a tampered copy is skipped,
  never used). Installer users can now register services fully offline, and
  the class of first-run failures on fresh Windows boxes — whose sparse
  root-certificate store makes Python's HTTPS verification fail even though
  the browser works — is gone entirely. The bundling step also re-verifies the
  pin on every release, so a wrong pin fails the build instead of a user's
  setup. (pip/source installs keep the verified download as the fallback.)
- **The installer also carries the Visual C++ runtime.** The other install-time
  download a fresh box needs — and the same class of machine that lacks the
  runtime is the one whose sparse certificate store can't download it. The
  installer now bundles `vc_redist.x64.exe` (Authenticode-verified at build
  time; the evergreen URL can't be hash-pinned) and runs it silently only when
  the runtime is actually missing. A brand-new Windows box now goes from
  `palctl-setup.exe` to a working server with zero downloads during install;
  the wizard's download path remains for portable/pip users.
- **The setup wizard re-opens at every launch until palctl is actually
  running.** It used to auto-open only when no config existed — so a setup that
  died partway (config saved, then a failed download or a refused service
  registration) stranded the user in a GUI wired to a daemon that isn't there,
  with no signpost back to the fix. The GUI now prompts until the daemon
  answers on its control port; an explicit "no background palctl" choice is
  respected and never nagged.
- **The installer's "register the palctl service now" checkbox is gone.** It
  could only ever register a LocalSystem daemon with no configuration — a
  half-setup that either fought the wizard's later registration or paired a
  SYSTEM daemon with a user-account server (the split that blinds the
  watchdog). The wizard is the one supported setup path; unattended
  deployments script `palctl-daemon install-service` instead.
- **The wizard defaults to the one correct install path and removes the wrong
  one from the menu.** "Run as a Windows service under your account" is now the
  pre-selected default, and while "Register the Palworld server as a Windows
  service" is ticked the login-startup option is greyed out with the reason —
  the server service and palctl must share one account, so the split that
  blinds the watchdog can no longer even be selected. Login startup remains
  available when palctl doesn't manage the server as a service (no split
  possible) — the setups, like PIN-only accounts, that genuinely need it.
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
