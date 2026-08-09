# Palworld dedicated server: documented practice, and what palctl does

Researched August 2026, after a report of the app "hanging up badly" turned out
to be several separate things — most of them documented behaviour of Palworld
and SteamCMD that palctl was on the wrong side of.

This page records what the sources actually say, and how palctl lines up with
each. It exists so the next person doesn't have to re-derive it, and so the
places where palctl deviates are a **choice on the record** rather than an
accident.

---

## 1. `validate` is for repair, not for updating

**What the sources say.** Valve's own SteamCMD documentation: *"it is recommended
you use this command only on initial installation and if there are server
issues"*, and — the sharp end — *"Validation will overwrite any files that have
been changed, which may cause issues with customized servers."* Files that are
not part of the default installation are left alone; files that are part of it
and differ get restored.

This is not a fringe reading. LinuxGSM exposes `validate` as its **own command**,
separate from `update`. SteamPS shipped an issue titled *"SteamCMD parameter
'validate' should not be default"* and moved it behind an opt-in `-Validate`
switch, for exactly this reason.

For Palworld specifically it is worse than generic: the community guidance is
that *"the Palworld SteamCMD flags set by the developers will have the
PalWorldSettings.ini file be wiped by SteamCMD"*, and one of the standard
mitigations is literally *"not to validate the server before starting"*.

**What palctl did.** Ran `app_update <id> validate` on **every** update — the
schedule, the GUI button, and Discord `/update`. Nothing ever passed
`validate=False`. So palctl performed a full multi-GB re-verification on every
routine update, and was itself the thing resetting `PalWorldSettings.ini`, then
tried to repair the damage afterwards.

**What palctl does now.** Routine updates run plain `app_update`, which still
installs the newest build. Validation is opt-in per call, as a repair:

```
POST /action/update-server  {"validate": true}
```

---

## 2. A server update can reset your settings — and that includes the ones
   palctl needs

**What the sources say.** Fields commonly reported as reset after a Palworld
patch include `ServerName`, `AdminPassword`, `ServerPassword`, `RCONEnabled`,
`RCONPort`, `MaxPlayers`, and the balance multipliers (`ExpRate`,
`PalCaptureRate`, `DeathPenalty`). The standard advice is to keep a copy of the
config and compare it after every update, because *"you will not be able to
restore the PalWorldSettings.ini file without a backup."*

Note what is in that list: `AdminPassword`. Palworld's REST API password **is**
`AdminPassword`, and the REST API is off by default. So a reset config doesn't
merely lose your tuning — it takes palctl's only channel to the server with it.
The server keeps running; palctl goes blind; restarting the service changes
nothing, because the process was never the problem.

**What palctl did.** Backed the ini up before every update, then restored it only
`if is_blank(...)` — only when the file came back *completely empty*. A reset
that leaves a valid file full of defaults sailed straight past that check, and
the backup sat on disk unused and unmentioned.

**What palctl does now.** A blank ini is restored wholesale, as before. A *reset*
ini has the admin's own values merged back over it — a merge, so settings a
genuine patch **added** keep their new defaults and settings it **removed** stay
removed. Then `RESTAPIEnabled` / `RESTAPIPort` / `AdminPassword` are re-asserted
after every update: the same idempotent call setup makes, which until now was
never made again for the life of the install. palctl reports which settings it
put back and where the pre-update copy is.

**Also worth knowing:** the server *itself* rewrites `PalWorldSettings.ini` from
its in-memory values when it shuts down. That is why palctl takes the pre-update
backup **after** stopping the server — the post-shutdown file is the truth — and
why editing the ini while the server is running is pointless.

---

## 3. `WorldOption.sav` silently overrides the ini

**What the sources say.** This is the single most common reason Palworld server
settings "don't apply". `PalWorldSettings.ini` is read when a world is first
**created**; after that the server copies the gameplay settings into
`WorldOption.sav` inside the save folder and reads them from there. *"If your
save folder contains a WorldOptions.sav file, the server entirely ignores your
PalWorldSettings.ini file."* Co-op and single-player worlds always ship with one,
so a save imported onto a dedicated server brings the override along.

Server-infrastructure settings — the server name, ports, player cap, the REST
API, passwords — keep coming from the ini. It is the gameplay/rate settings that
get shadowed.

The failure mode is the nasty kind: the edit saves without error, and the server
ignores it.

**What palctl did.** Mentioned it in a sentence inside a paragraph shown to
everyone, whether or not the file existed — noise for the servers it doesn't
apply to, and easy to skim past on the ones where it silently discards edits.

**What palctl does now.** The settings editor looks for `WorldOption.sav` under
the save folder and, only when one is actually there, says so prominently: which
settings will be ignored, which still work, and the full path to the file.

---

## 4. Restarts are the accepted mitigation for the memory leak — and they are not
   the only one

**What the sources say.** Palworld's dedicated server has leaked memory since
launch; the 1.0 update did not fix it, and the behaviour persists: the longer the
process runs, the more RAM it holds. Common guidance is a scheduled restart every
12–24 hours, more often for busy 32-player worlds or smaller boxes.

The part worth flagging: *"the combination of disabling invaders and scheduling
restarts is what keeps most Palworld servers running, with neither fix alone as
effective as both together."* `bEnableInvaderEnemy=False` is a widely-recommended
companion to restarts, not an alternative to them.

**Where palctl stands.** palctl's whole pitch is restarting on the *symptom*
(actual resident memory read from the OS) rather than the clock, which is
strictly better than a fixed timer and is the right call. `bEnableInvaderEnemy`
is already editable in the settings editor.

The reported mechanism is specific rather than folklore: the scripted invader
waves spawn enemies the server holds and never cleans up, and operators report
RAM climbing at roughly half the rate with them off. Independent hosting
providers agree on this; Pocketpair has not published anything on it, so treat
the "halves it" figure as operator experience rather than a measurement.

**palctl surfaces it, and deliberately does not act on it.** The settings editor
explains it on `bEnableInvaderEnemy` itself, and the leak forecast — the one
moment an admin is definitely thinking about memory — appends the suggestion
*when raids are actually on*. palctl does **not** turn raids off by itself, at
setup or anywhere else: raids are game content, the evidence is operator
report rather than vendor guidance, and a server manager that silently changes
how someone's game plays is the same class of mistake as a config editor that
drops keys it didn't recognise. Surfacing it puts the decision where it
belongs.

**The account split matters more than the restart cadence.** If the server runs
under a different Windows account than palctl (server as SYSTEM, palctl as you),
psutil can't read the server's memory: it lands on the idle ~7 MB launcher, the
leak watchdog never fires, and the box climbs until it thrashes. palctl warns
about this — but the warning used to be suppressible by a single unreadable
poll, which is now fixed (the split is itself a reason the read fails, so the
boxes that needed the warning were the least likely to get it). The installer's
"Path A" registers both services under one account. Check this first on any box
where the leak seems unmanaged.

---

## 5. The REST API is the right interface, and must not be exposed

**What the sources say.** The REST API is Pocketpair's documented admin
interface, enabled with `RESTAPIEnabled=True`, defaulting to port 8212, and
authenticated with the admin password. The documentation is explicit that *"these
APIs are not designed to be exposed directly to the Internet, as publishing
directly to the Internet may result in unauthorized manipulation of the server."*

**Where palctl stands.** Correct on both counts: it drives the REST API rather
than deprecated RCON, and only ever talks to `127.0.0.1`. palctl's own control
API is separately localhost-bound and token-gated. The dashboard's LAN mode is
opt-in and documented with the tradeoff.

---

## Sources

- [SteamCMD — Valve Developer Community](https://developer.valvesoftware.com/wiki/SteamCMD)
- [SteamPS issue #33 — "SteamCMD parameter `validate` should not be default"](https://github.com/hjorslev/SteamPS/issues/33)
- [LinuxGSM — `validate` command](https://docs.linuxgsm.com/commands/validate)
- [Palworld Server Guide — REST API](https://docs.palworldgame.com/api/rest-api/palwold-rest-api/)
- [Nodecraft — Fix Palworld server update wiping player progress](https://nodecraft.com/support/games/palworld/fix-palworld-server-update-wiping-player-progress)
- [WinterNode — Palworld server updates: handling breaking patches](https://winternode.com/blog/palworld/palworld-server-updates-how-to-handle-breaking-patches-witho)
- [LOW.MS — WorldOption.sav vs PalWorldSettings.ini](https://low.ms/knowledgebase/palworld-worldoption-sav-vs-palworldsettings-ini)
- [Apex Hosting — How to fix Palworld server settings not being applied](https://apexminecrafthosting.com/guides/palworld/how-to-fix-palworld-server-settings-not-being-applied/)
- [legoduded/palworld-worldoptions — building a WorldOption.sav from an ini](https://github.com/legoduded/palworld-worldoptions)
- [Connect Hosting — Palworld memory leak fix](https://connecthosting.net/blog/palworld-memory-leak-fix)
- [Shockbyte — Palworld server known issues & common fixes](https://shockbyte.com/help/knowledgebase/articles/palworld-server-known-issues-common-fixes)
