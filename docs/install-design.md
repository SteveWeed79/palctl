# Install & service-lifecycle design

Why palctl's install code looks the way it does, and the rules it must keep
obeying. Written after an audit that found (and fixed) a series of reinstall
and mode-switching bugs; the point of this document is that nobody has to
rediscover these the hard way.

## Why install code is a bug magnet

palctl deliberately does **runtime self-installation**: the wizard and the CLI
register services themselves, so a non-technical host never opens an admin
terminal. That's the right UX for this audience — but it means palctl re-owns
problems that installer frameworks (MSI, Inno service directives, distro
packaging) solved decades ago. Four structural traps come with that choice:

1. **Installation is a state machine, not a script.** "Do the install steps in
   order" is correct exactly once — on a clean machine. The real requirement is
   *reconciliation*: any prior state (service registered, Run key set, daemon
   running, mode switched, wrong elevation) must converge to the desired state.
   Every install bug found in the audit was a missing transition:
   running → reinstalled, login → service, something → none.

2. **The OS lies by omission.** `systemctl start` on an active unit,
   `nssm install` on an existing service, `sc start` on a running one — all
   silently succeed while doing nothing. Code that ignores exit codes and
   states never observes its own failure.

3. **The service manager is asynchronous.** Stops and deletions *land after
   the command returns*. Windows keeps a removed service in "pending deletion"
   while anything holds a handle to it (an open `services.msc` is enough), and
   re-creating the name in that state fails.

4. **Install code runs rarely and mutates global state with no rollback.**
   Bugs here don't die; they hibernate until the next reinstall.

## The rules

These are enforced by unit tests (`test_winservice.py`, `test_systemd.py`,
`test_daemon_helpers.py`, `test_setup_flow.py`) and by the `install-lifecycle`
CI job, which exercises the real state machine against a real Windows SCM.

### 1. Reinstall replaces; it never patches

* **Windows:** stop → wait for STOPPED → delete → wait until the name is
  actually gone → register from a freshly written config → start. The WinSW
  config XML is the *whole truth*, rewritten every install, so stale settings
  can't survive by construction. If the name never frees ("pending deletion"),
  raise with the cause instead of configuring a zombie.
* **Linux:** the unit file is fully rewritten (write **is** the reinstall),
  then `daemon-reload` → `enable` → **`restart`** (never `start`, which no-ops
  on an active unit). This mirrors Debian's own packaging convention:
  `dh_installsystemd` generates `restart` on upgrade, `start` on fresh install.

### 2. Exactly one startup mechanism owns the daemon

Three modes exist: **service** (WinSW / systemd), **login startup** (HKCU Run
key, Windows only), and **none**. Switching modes removes the other mechanism
*first* — order matters:

* login → service: drop the Run key, register the service **stopped**, kill
  whatever still holds the control port, then start. (Starting first would
  lose the port to the old daemon and restart-loop the new one.)
* service → login: remove the service **before** killing the daemon process —
  a service manager resurrects anything its service owns, and the resurrected
  copy would fight the fresh daemon for the port.
* → none: remove **both** mechanisms and stop the running daemon. Unticking
  must actually turn the thing off (the same contract the Discord toggle
  documents in `setup_flow.py`).

The chosen mode is persisted (`Config.daemon_startup`), so a wizard re-run
defaults to what the user actually picked instead of silently switching back.

### 2b. One Windows account owns both services ("Path A")

The daemon and the game server must run under the **same Windows account**.
A split — the classic default was PalServer as a LocalSystem service with the
daemon on login startup — means palctl cannot reliably read the server
process: metrics silently degrade to the idle few-MB bootstrap launcher and
the memory watchdog can never fire. Three layers enforce this:

* `setup_flow.would_split_accounts` is the single shared rule; `run_setup`
  **refuses** any plan that would split (the wizard applies the same rule and
  greys the login option out while palctl manages the server as a service).
* The wizard's default registers **both** services under the invoking user
  (`SetupPlan.service_password`); the account password is handed to the SCM at
  registration and **scrubbed from the WinSW XML immediately after** — it must
  never persist on disk (NSSM stored nothing; WinSW's config file would).
* At runtime the daemon compares the server process owner against its own
  account (`procs.server_account_mismatch`) and warns loudly on a mismatch —
  the backstop for states created outside setup (hand-edited services, CLI
  combinations, old installs).

Login startup remains only where no split is possible: palctl not managing
the server as a service, or PIN-only/passwordless accounts that cannot hold a
service logon at all (Error 1069).

### 3. Success is verified, never assumed

The service manager's opinion is not the success signal — **the daemon's own
control port answering is**. `install_service` and `start_detached` return a
verified result after a bounded wait, and failure messages say where to look
(`palctl-daemon run` in a console on Windows; `systemctl status` /
`journalctl -u` on Linux). The CLI install commands exit nonzero on verified
failure so scripts and CI can assert outcomes.

### 4. Elevation is demanded when it's actually needed

Registering a service needs admin — and so does **removing** one, which means
switching *away* from a registered service (to login startup or none) needs
admin too. Preflight (`setup_flow.needs_admin`) checks the real system state,
not just the requested mode; an unelevated removal that silently fails would
leave the old daemon running while setup claims success.

### 5. Nothing is downloaded at install time; what is downloaded is verified

The install-time download was itself the defect: the machines that need a
first-run most — fresh Windows boxes — are exactly the ones whose sparse
root-certificate store fails Python's HTTPS verification (Python never
triggers CryptoAPI's on-demand root fetch the way browsers do), and AV
HTTPS-scanning breaks the same call on seasoned boxes. So:

* **WinSW and the VC++ redistributable ship inside the build** — downloaded
  and verified once by the release build (WinSW against the SHA-256 pin;
  VC++ by Authenticode, since the evergreen URL can't be pinned), which also
  makes every release an independent re-verification of the pin: a wrong pin
  fails the build, never a user's setup. `ensure_winsw` prefers the bundled
  copy, then the cache, then the network — and **verifies against the pin at
  every step**, including a cached or manually dropped copy (anything in that
  cache becomes a SYSTEM service binary; trust nothing on sight).
* Remaining runtime downloads (SteamCMD; the wizard's fallback paths for
  pip/portable users) go through `fetch.open_url`: system trust first, one
  certifi retry on a certificate-verification failure, fail **closed** with a
  message naming the cause. Verification is never disabled.
* Anything setup must download is fetched **before** the first byte of
  config/ini/secret is written, so a blocked download aborts a setup that
  changed nothing (`run_setup` fetches the wrapper up front).

## Why WinSW (and not NSSM)

NSSM's last release was 2014; it is unmaintained. Beyond maintenance, the
decisive property is **declarative config**: NSSM stores each setting
individually in the registry, and its `install`/`set` commands only overwrite
what the *current* invocation specifies — which is how re-installs inherited a
stale service account or stale launch arguments. WinSW's one-XML-file model
makes that class impossible. WinSW also grants the "Log on as a service" right
itself (`<allowservicelogon>`), removing one cause of Error 1069 for
`--as-user` installs.

Removal and start/stop go through plain `sc.exe`, which works on *any*
service — so palctl versions that used NSSM migrate cleanly: the next install
stops and deletes the old NSSM-wrapped service and registers the WinSW one.

**Two WinSW traps NSSM never had** (found by auditing the conversion; both
handled — keep them handled):

* WinSW takes the service-account password via its **XML config file**. NSSM
  passed it straight to the SCM and stored nothing. The XML must be scrubbed
  immediately after `install` (the SCM holds the credential, encrypted, from
  that point) — see `install_service`.
* WinSW's `<stoptimeout>` **kills** the process when it expires — a flat
  guillotine where NSSM had an escalation ladder. The game service gets 90s
  (PalServer flushes the world on the way down; palctl's own stop paths wait
  that long), the daemon keeps the 30s default.

## The layers

| Layer | Owns | Files |
|---|---|---|
| Runtime self-install | services, Run key, mode switching, verification | `winservice.py`, `systemd.py`, `startup.py`, `daemon.py` (install/uninstall/`start_detached`) |
| Setup orchestration | ordering, preflight, persistence, user-facing log | `setup_flow.py` (+ wizard as a thin view) |
| Packaged installer | file copy, upgrades over a running daemon, uninstall | `packaging/installer.iss` |

The Inno installer must keep stopping the service **and** killing a login-mode
daemon before file copy (both hold the exe open), and restarting the right one
after — see the comments in `installer.iss`. Known accepted limitation: its
HKCU Run-key probe runs in the elevated user's hive, so the
standard-user-plus-admin-credentials case won't auto-restart a login-mode
daemon after upgrade.

### What uninstall removes — and what it deliberately doesn't

`[UninstallRun]` removes everything palctl owns of *itself*: the
**palctl-daemon** service, the login-startup Run key, any running daemon/GUI
process, the dashboard firewall rule, and the health task. What it does **not**
touch, by design:

* **The PalServer service.** It is a *separate* service the wizard registered
  for the user's game server; removing palctl should not silently deregister
  the thing that keeps their world online. A user who wants it gone runs
  `sc delete PalServer` (or unticks the server-service option and re-runs the
  wizard). This is intentional, not an oversight — but there is no palctl
  command that removes only the PalServer service today, so a full manual
  cleanup means one `sc.exe` line.
* **The config directory** (`%APPDATA%\palctl`), which holds `config.json`, the
  daemon state, backups metadata, logs, and the per-service WinSW wrapper copies
  under `bin\`. Config is preserved so a reinstall keeps the user's setup;
  `uninstall-service` already unlinks the daemon's own wrapper pair, but the
  PalServer wrapper copy stays as long as that service does.

If uninstall is ever made to remove the game server too, it must go through
`sc.exe stop`/`delete` (WinSW may be gone) and prompt first — silently stopping
someone's live server on an app uninstall is the wrong default.

## Sources

* [dh_installsystemd — restart-on-upgrade convention](https://manpages.debian.org/testing/debhelper/dh_installsystemd.1.en.html)
* [sc delete — Microsoft Learn](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/sc-delete)
* [Error 1072 "marked for deletion" — causes and cleanup](https://troubleshooter879859767.wordpress.com/2020/12/21/1072-the-specified-service-has-been-marked-for-deletion/)
* [kardianos/service — Install() errors on existing service by design](https://pkg.go.dev/github.com/kardianos/service)
* [tailscaled — daemon-owned install/uninstall commands](https://tailscale.com/docs/reference/tailscaled)
* [WinSW CLI commands](https://github.com/winsw/winsw/blob/v3/docs/cli-commands.md)
* [Servy vs NSSM vs WinSW — wrapper landscape](https://dev.to/aelassas/servy-vs-nssm-vs-winsw-2k46)
* [Inno Setup AppMutex](https://jrsoftware.org/ishelp/topic_setup_appmutex.htm)
