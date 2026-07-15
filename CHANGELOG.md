# Changelog

All notable changes to palctl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come
straight from git tags (setuptools-scm), so the tag *is* the version.

Installers for every release are on the
[Releases page](https://github.com/SteveWeed79/pal-it-up/releases), each with a
`SHA256SUMS.txt` to verify the download.

## [Unreleased]

### Added
- **Cloud / off-site backup mirror via rclone.** The backup mirror now accepts
  an [rclone](https://rclone.org) remote (`remote:path`, e.g.
  `gdrive:PalworldBackups`) in addition to a local path, so backups can be
  pushed off the box to Google Drive, Dropbox, S3, OneDrive, and anything else
  rclone speaks. palctl shells out to the rclone binary the user configured
  with `rclone config` — it never handles OAuth tokens or a cloud API itself.
  Uploads are idempotent and pruned to the same retention as local backups, a
  mirror failure never fails the primary backup, and the daemon warns at
  startup if a remote is configured but rclone isn't installed.

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

[Unreleased]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.13...HEAD
[0.1.13]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.12...0.1.13
[0.1.12]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.11...0.1.12
[0.1.11]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.10...0.1.11
[0.1.10]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.9...0.1.10
[0.1.9]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.6...0.1.9
[0.1.0]: https://github.com/SteveWeed79/pal-it-up/releases/tag/0.1.0
[0.1.6]: https://github.com/SteveWeed79/pal-it-up/compare/0.1.0...0.1.6
