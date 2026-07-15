# Changelog

All notable changes to palctl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come
straight from git tags (setuptools-scm), so the tag *is* the version.

Installers for every release are on the
[Releases page](https://github.com/SteveWeed79/pal-it-up/releases), each with a
`SHA256SUMS.txt` to verify the download.

## [Unreleased]

### Changed
- **A failed pre-update backup now aborts the server update** (manual,
  scheduled, and Discord-triggered) instead of warning and updating anyway —
  updates are exactly when saves get corrupted, and without that backup a bad
  update can't be rolled back. A fresh install with no world yet still updates
  freely, and the old warn-and-continue behaviour is available by unticking
  **Update requires a backup** in Config.

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
- **Hot backups are now consistency-checked.** A backup taken while the server
  is running fingerprints the world before and after the copy; if the server
  wrote mid-copy (a potentially torn backup), the copy is retried in a quiet
  window. If no quiet window is found the backup is kept but flagged, with a
  warning suggesting a clean neighbour for restores.
- CI now installs `aiohttp` and `discord.py` for the test job, so the daemon's
  auth-token-gate and crash-auto-recovery tests actually run instead of being
  silently skipped on every push.

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

[Unreleased]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.13...HEAD
[0.1.13]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.12...0.1.13
[0.1.12]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.11...0.1.12
[0.1.11]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.10...0.1.11
[0.1.10]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.9...0.1.10
[0.1.9]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.6...0.1.9
[0.1.0]: https://github.com/SteveWeed79/pal-it-up/releases/tag/0.1.0
[0.1.6]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.0...0.1.6
