# Improvement plan — what the field does that palctl doesn't

A competitive review of comparable server managers, turned into a ranked plan.

**Method.** Five agents read palctl's own source and produced a capability map with
`file:line` evidence (README claims were treated as claims, not evidence). Eight more
researched a comparable tool or practice domain from primary sources — repositories,
scripts, unit files and config schemas rather than marketing pages. Everything in
Tier 1 below was then re-verified by hand against this repo; those are marked ✅.
Items marked 📋 rest on the inventory agents' cited evidence and are worth a
second look before implementation.

**Sources.** [Pterodactyl](https://github.com/pterodactyl/panel) ·
[AMP templates](https://github.com/CubeCoders/AMPTemplates) ·
[LinuxGSM](https://github.com/GameServerManagers/LinuxGSM) ·
[palworld-server-docker](https://github.com/thijsvanloef/palworld-server-docker) ·
[itzg/docker-minecraft-server](https://github.com/itzg/docker-minecraft-server) ·
[itzg/docker-mc-backup](https://github.com/itzg/docker-mc-backup) ·
[itzg/mc-monitor](https://github.com/itzg/mc-monitor) ·
[WindowsGSM](https://github.com/WindowsGSM/WindowsGSM) ·
[MCSManager](https://github.com/MCSManager/MCSManager) ·
[palworld-save-tools](https://github.com/cheahjs/palworld-save-tools) ·
[restic](https://github.com/restic/restic) · [borg](https://github.com/borgbackup/borg) ·
[kopia](https://github.com/kopia/kopia)

---

## Where palctl already leads

Worth stating first, because it constrains what's worth copying.

- **Symptom-driven restarts.** Nothing else in the survey restarts on measured
  resident memory with player hold-off, a hard limit and leak *forecasting*.
  The field restarts on a clock (`AUTO_REBOOT_CRON_EXPRESSION`) or on a crash.
- **A pure decision engine with a recorded "why".** `supervisor.decide()` plus the
  decision log answers "why isn't palctl doing anything" — panels generally can't.
- **Boot-ownership handling.** Registering the game server Manual, starting it only
  if that's how you left it, and handing the boot job *back* on uninstall. No other
  tool surveyed models who owns boot at all.
- **REST-native.** Everything else drives RCON or scrapes stdout. It's why palctl has
  FPS, frame time, per-player ping and building counts.
- **One operation lock.** AMP and Pterodactyl have per-server processing locks;
  LinuxGSM has a lockfile taxonomy. palctl's is comparable and better documented.

---

## Tier 1 — data-safety defects (all small, all verified)

These aren't features. They're places where palctl reports success while doing
something less useful than it claims.

### 1.1 A backup is taken even when the pre-backup save failed ✅

`_do_backup` calls `save_best_effort(settle=3)` and **discards the boolean**
(`scheduler.py:208`). `save_best_effort` returns `False` when the API doesn't answer
(`control.py:112-121`). So when the REST API is wedged — exactly when you most want a
good backup — palctl copies a stale world and files it as a successful backup.

Every comparable tool guards this explicitly: palworld-server-docker refuses to shut
down or back up on a failed save, LinuxGSM stops the server first, mc-backup runs
`save-off` / `save-all` / `sync` and *installs the `save-on` trap first*.

**Do:** branch on the return. On `False`, either skip with a loud event (consistent
with the disk-space guard immediately below it, which already does exactly this) or
label the backup `unflushed` so a restore warns. Also replace the fixed `settle=3`
with a wait for the save to actually land — mtime settling on `Level.sav` — which is
palworld-server-docker's `backup-save-settle` behaviour.

**Effort:** S. `scheduler.py`.

### 1.2 `sessions.db` is copied hot into every backup ✅

`backups.py:161` says so in its own comment: *"sessions.db is copied hot (the daemon
may be writing)"*. A byte-for-byte copy of a live SQLite file is not guaranteed to be
a valid database — it can land mid-transaction and restore as corrupt, and it fails
silently until the day you need it.

**Do:** `VACUUM INTO` (SQLite 3.27+, so every supported Python) or the
`sqlite3.Connection.backup()` API. One line, removes a whole class of surprise.

**Effort:** S. `backups.py`.

### 1.3 Nothing verifies a backup is restorable ✅

`is_restorable()` (`backups.py:227`) checks the name resolves to a directory. That's
it. There is no integrity check and no restore drill.

The backup-engineering field treats this as the central discipline: restic and kopia
distinguish structural `check` from `check --read-data`, amortise the expensive scrub
across runs, and hold that *a backup nobody has restored is not a backup*. The
Palworld ecosystem offers a shortcut here — a `.sav` carries a 12-byte header that
detects truncation without parsing the file.

**Do, in ascending order of ambition:**
1. On `create`, record a manifest: file count, total bytes, per-file size, and the
   `.sav` header check on `Level.sav` and each player `.sav`. Write it into the
   backup. `is_restorable` then means something.
2. A `palctl backup verify [NAME]` command and a Discord `/verify`.
3. A scheduled, rate-limited **restore drill**: restore the newest backup into a temp
   directory, run the manifest check, delete it, emit the result. Weekly is plenty.

**Effort:** S for (1), M for (3). `backups.py`, `scheduler.py`.

### 1.4 No persisted last-backup time — so a missed backup is invisible ✅

Grepping `last_backup|last_run|next_backup` across `palctl/*.py` returns nothing. The
backup schedule is a sleep-first interval anchored to daemon start, so a daemon that
restarts more often than the backup interval **never backs up at all**, and nothing
notices. The README promises "backups that always run, at least once a day".

The backup field calls the fix a dead-man's switch: alert on the backup that *didn't*
happen, not only on the one that failed.

**Do:** persist `last_backup_at` in `daemon_state.json`; compute the next run from it,
not from process start; emit a rising-severity alert once the gap exceeds the
configured interval by some margin. Same treatment for the off-site copy.

**Effort:** S–M. `scheduler.py`, `config.py`.

### 1.5 Off-site backups can be uploaded but never retrieved ✅

`rclone.py` has `mirror`, `listing`, `prune`, `test_remote`, `check` — and no download
(`rclone.py:97-160`). The restore path only reads the local backup root. So the copy
that exists for the case where *the box is gone* cannot be restored by palctl, and the
config snapshot ride-along (`palctl-config.zip`) has no restore path either.

**Do:** `rclone.pull(remote_name, dest)`; teach `restore` to accept a remote backup
name and stage it locally first; add a `palctl restore --from-offsite`. Then verify
after upload rather than trusting rclone's exit code — compare size and count against
the local manifest from 1.3.

**Effort:** M. `rclone.py`, `scheduler.py`, `cli.py`.

### 1.6 A crashed worker loop is never restarted ✅

`daemon.py:1598` emits *"crashed and is disabled until restart"* and leaves it there.
The daemon keeps running, `/healthz` keeps answering, and a subsystem — the watchdog,
the scheduler, the poll loop — is simply gone. This is the silent-failure shape the
whole codebase is otherwise good about.

**Do:** restart the worker with exponential backoff and a bounded budget (the field's
standard: mc-server-runner's crash budget, Pterodactyl's crash-with-backoff). After
the budget, mark the daemon degraded and say so in `/state` and `/healthz` — a daemon
missing its watchdog should not report healthy.

**Effort:** S–M. `daemon.py`.

### 1.7 SteamCMD's own archive is downloaded without an integrity check — *withdrawn*

The observation is true (`steamcmd.py:289` fetches over TLS and extracts, while the
WinSW wrapper in the same codebase is SHA-256 pinned on all three acquisition paths),
but the recommended fix was wrong and is withdrawn rather than implemented.

WinSW is pinnable because palctl pins a *version*: a specific release asset that never
changes. Valve publishes SteamCMD at a single rolling URL whose contents change
whenever they update it, with no versioned archives and no published checksums. A
pinned hash there would not harden the download; it would break every install the
first time Valve rebuilt the archive, and the pressure would then be to bump the
constant without checking anything — security theatre with an outage attached.
LinuxGSM, which is careful about this class of thing, does not pin it either.

What is worth doing instead is narrower and is **not** yet done: after extraction,
confirm the binary is what it claims to be before executing it (a real PE/ELF of
plausible size, not an HTML error page a captive portal returned with a 200), and
record its hash on first install so a *change* between runs can be reported. That is a
tamper-evidence measure, not a pin. Filed as future work.

---

## Tier 2 — capability gaps against the field

### 2.1 No build pinning, no rollback, no branch 📋

`steamcmd.py` parses both the installed build id (from `appmanifest_*.acf`) and the
latest, which is most of the work — but `update_command` takes no `-beta`, no
`betapassword` and no manifest id, so there is no way to hold a server on a known-good
build or go back to one after a bad patch.

This is the single most-copied idea in the Palworld ecosystem:
palworld-server-docker ships `TARGET_MANIFEST_ID` plus a maintained
version→manifest table in its docs, and falls back to DepotDownloader when SteamCMD
can't do it. For a game that patches as often as Palworld and has shipped
save-breaking builds, "put it back the way it was" is the feature.

**Do:** add `branch`/`beta_password`/`pin_manifest_id` to config and thread them into
`update_command`; surface `palctl update --pin <id>` and `palctl rollback`; record the
build id in every backup's manifest so a restore can tell you which build wrote it.

**Effort:** M. `steamcmd.py`, `scheduler.py`, `config.py`, `cli.py`.

### 2.2 Updates take the server down with no in-game warning ✅

`_COUNTDOWN_OPS = frozenset({"restart", "restore"})` (`scheduler.py:37`) — update is
deliberately excluded. Every comparable tool warns:
`AUTO_UPDATE_WARN_MINUTES` in palworld-server-docker, announce-then-stop task chains
in Pterodactyl and AMP, player-deferred restarts in LinuxGSM.

palctl already has the whole countdown machine, with cancel and skip, and it already
collapses to a few seconds on an empty server. Adding `"update"` to that set is close
to a one-word change plus the plumbing to make cancel mean something.

**Effort:** S. `scheduler.py`.

### 2.3 The scheduled update fires whether or not there's an update ✅

`_auto_update_loop` (`scheduler.py:459-483`) checks `enabled`, `auto_update`, and
"intentionally stopped" — then calls `update_server()` directly. It never consults
`check_update_available()`, which is sitting right below it at line 486 and already
maintains `update_status`.

So a nightly auto-update stops, updates and restarts the server every night, in front
of whoever is playing, on the majority of nights when Palworld shipped nothing.
WindowsGSM and palworld-server-docker both gate on a build-id diff and both
*fail closed* when the lookup fails.

**Do:** call `check_update_available()` first; skip with a quiet event when there's
nothing; on an inconclusive check, skip rather than update — a failed lookup is not
evidence of a new build.

**Effort:** S. `scheduler.py`.

### 2.4 No actor attribution, no audit trail 📋

Events record that a restart, stop, kick or ban happened, never who asked or from
which surface (`events.py`). With a Discord bot, a web dashboard, a CLI and a GUI all
able to stop the server, "who stopped it" is unanswerable — and it's the first
question after a surprise.

Pterodactyl writes one activity log from both the panel and the daemon, carrying
actor, IP, API key and before/after values.

**Do:** add `actor` and `via` to `Event`; populate at every entry point; show them in
`/events`, the dashboard and `palctl events`. Cheap, and it makes the event feed an
audit trail instead of a status feed.

**Effort:** S–M. `events.py` plus each surface.

### 2.5 Alerting is one webhook with a fixed event set, and failures are silent 📋

`_kinds` is assigned once in `__init__` and `reconfigure()` doesn't revisit it; a
Discord send that raises is swallowed with a bare `pass`.

The field's pattern, from LinuxGSM: a **closed event vocabulary** rendered by
interchangeable backends, every alert carrying a diagnostic snapshot taken at the
moment it fired, and a `test-alert` command that doubles as a config validator —
naming the missing field and linking its documentation.

**Do:** log delivery failures (silence here is the same bug class as Tier 1); make the
event set configurable; add `palctl test-alert`; attach a small diagnostic snapshot to
watchdog and crash alerts. Consider [Apprise](https://github.com/caronc/apprise) to
get ntfy, Telegram, Gotify, email and Slack for roughly the cost of one integration.

**Effort:** M. `alerts.py`, `bot.py`, `cli.py`.

### 2.6 Backups sitting on the same volume as the server, unremarked 📋

`backups.py` discusses this hazard in its own docstrings but never checks
`st_dev`. A one-comparison warning at setup and in preflight turns a documented
principle into an enforced one. **Effort:** S.

### 2.7 Retention is a flat count; the field uses two axes 📋

`prune(backup_root, retain)` keeps N. restic/borg/kopia all express retention as
grandfather-father-son on calendar boundaries, and mc-backup prunes on age *and*
count. A flat count means a burst of manual backups can evict every daily you had.

**Do:** keep-last / keep-daily / keep-weekly / keep-monthly, ORed, with a floor that
can never delete the only backup. **Effort:** M. `backups.py`.

---

## Tier 3 — bigger bets

### 3.1 Save-bloat surgery — the highest-value thing palctl could own

`Level.sav` grows without bound as players quit and guilds empty out. The result is
minute-long saves, long restarts, and eventually a world that won't load. It is *the*
Palworld server failure mode, and palctl currently treats only its symptom (the memory
watchdog).

The ecosystem's answer is
[palworld-save-tools](https://github.com/cheahjs/palworld-save-tools)-style pruning:
remove inactive players, empty guilds, dead base camps and unreferenced containers
from `Level.sav`. Operators do this by hand, offline, terrified, having read a forum
post.

palctl is uniquely placed to do it *safely*: it is on the box, it already knows exactly
who has played and when (`sessions.db` — which no other tool has), it already has the
operation lock, and it already takes backups. That combination is unavailable to every
cloud panel in this survey.

**Do:** vendor `palworld-save-tools` as an optional dependency; run the parse
**out of process** (it is memory-hungry and the daemon must not die with it); gate the
whole operation behind a fresh verified backup; drive the "inactive" decision from
`sessions.db` rather than asking the operator to guess; keep a persistent exclusion
list so a returning player isn't pruned twice; report bytes reclaimed. Offer it as
"your world is 3.2 GB and 60% of it is players who last logged in 90 days ago".

**Effort:** L. New module. **Value:** this is the feature people would switch for.

### 3.2 Auto-pause when the server is empty

palworld-server-docker suspends the server process with `SIGSTOP` once empty (gated on
a successful save) and wakes it on the first inbound packet via NFLOG, with a knockd
fallback; it even replays the community-browser heartbeat so a paused server stays
listed. itzg's stack models the same idea as an explicit five-state machine that fails
safe toward "not paused", with a `.paused` flag file so other components can
coordinate, and a health check that knows about the manager's deliberate states.

palctl already knows the player count and owns the process. The Windows equivalent of
`SIGSTOP` is process suspension via `NtSuspendProcess`; waking on a packet is the hard
part on Windows.

**Do it as a Linux-first opt-in**, with the state machine and the pause-aware health
check first — those are the parts that make it safe. **Effort:** L.

### 3.3 Headless Linux install path 📋

`setup_flow.run_setup` is deliberately Qt-free and contains the entire install
sequence — and its only caller is the Qt wizard. The README advertises headless Linux;
a headless Linux user cannot run setup. Similarly, `_register_server_service` is
WinSW-only, so palctl never creates a systemd unit for the thing it supervises.

**Do:** `palctl setup` as a CLI front end onto the existing plan/apply structure, and a
systemd unit generator for the game server mirroring the Windows path. This is mostly
wiring — the logic exists. **Effort:** M.

### 3.4 Metrics export

Seven days are retained; no surface shows more than about two hours, and there is no
export. mc-monitor's shape is the right template: a small, well-labelled metric set
collected at scrape time and exposed over more than one transport.

**Do:** `/metrics` in Prometheus text format behind the existing token. Cheap, and it
hands power users Grafana without palctl having to build charts. **Effort:** S–M.

---

## Deliberately not adopting

Recording these so they don't come back around:

- **Multi-tenancy, subusers, per-server permission trees, billing, node allocation**
  (Pterodactyl, AMP, MCSManager). palctl is one operator, one server, on their own box.
  A read-only co-admin token is the most of this idea worth having.
- **Docker/container isolation** (Pterodactyl, Pelican). Directly opposed to palctl's
  reason for existing: it must see the real process to read its real memory.
- **Console stdout scraping and regex state contracts** (AMP, WindowsGSM, LinuxGSM).
  palctl has the REST API, which is strictly better. Palworld also ships no log file.
- **Per-game plugin/egg/template systems.** palctl is one game deliberately; the whole
  value is Palworld-specific knowledge that a generic template can't hold.
- **Env-var-to-INI compilation** (palworld-server-docker). palctl's typed settings
  editor that preserves unknown keys and comments byte-for-byte is the better answer.

---

## Status

Done, with tests that fail against the pre-fix source:

| Item | What shipped |
|---|---|
| 1.1 | A failed pre-backup save is announced and recorded in the manifest |
| 1.2 | `sessions.db` snapshotted through SQLite's backup API |
| 1.3 | Per-backup manifest, `verify()`, and a `.sav` truncation check |
| 1.4 | Backup schedule measured from the newest backup; stale-backup warning |
| 1.5 | `rclone.pull()` — off-site backups can be retrieved |
| 1.6 | Crashed worker loops restart with backoff; `degraded` in `/healthz` |
| 2.3 | Auto-update checks for an update first, and fails closed |
| 1.7 | Withdrawn — see above |
| 2.1 | Steam branch/beta selection (`-beta`), config-driven — *partial*, see below |
| 2.2 | Updates run the countdown, with cancel and skip |
| 2.3 | Auto-update checks for an update first, and fails closed |
| 2.5 | Discord delivery failures are logged instead of swallowed — *partial* |
| 2.6 | Setup warns when backups sit on the server's own disk |
| 2.7 | Grandfather-father-son retention, layered over the flat count |
| 2.4 | Actor attribution on every event, via a ContextVar |

**2.1 is partial and honestly so.** `-beta <branch>` holds a server on a named
branch, which covers "don't take today's build". It is *not* a version pin:
pinning to an exact depot manifest id (what `TARGET_MANIFEST_ID` does in
palworld-server-docker) requires `download_depot` or DepotDownloader rather
than `app_update`, which is a different install path and a larger change. So
rollback to an arbitrary previous build is still not possible from inside
palctl. Recording the installed build id in each backup manifest — so a restore
can say which build wrote that world — is also still open.

**2.5 is partial.** Delivery failures are no longer silent, but the event set is
still fixed at construction (`_kinds` is assigned once and `reconfigure()`
doesn't revisit it), there is still no `test-alert` command, and alerts carry no
diagnostic snapshot. A notification-library abstraction (Apprise, shoutrrr) is
untouched.

**2.4 is a label, not an identity.** The control token is one shared per-user
secret, so every holder is already fully authorised; `actor`/`via` say which
surface and which person *claimed* to be asking, which is what makes the feed
readable. It is not authentication and must never be treated as such — a real
audit trail needs per-user credentials, which is the 2.4-adjacent work still
open (see "Auth is one shared secret" in the surfaces inventory).

Not yet done: all of Tier 3.

## Suggested order

1. **Tier 1 as one PR** — 1.1, 1.2, 1.4, 1.6 are each a few lines and each closes a
   silent failure. 1.3's manifest, 1.5's pull, and 1.7's hash follow naturally.
2. **2.2 + 2.3 as one small PR** — both are in `scheduler.py`, both are visible to
   players the day they ship.
3. **2.1 build pinning** — the highest-value Tier 2 item; the parsing already exists.
4. **3.1 save-bloat surgery** — scoped as its own project, on top of the verified
   backups from 1.3.

---

## Caveat on completeness

The research fan-out was stopped early for cost: eight of twelve planned research
topics completed (operator-experience patterns, observability, distribution/trust and
part of the panel survey did not), and the automated gap-check phase did not run —
Tier 1 was verified by hand instead, and 📋 items rest on cited inventory evidence.
Nothing here should be treated as an exhaustive survey of the field.
