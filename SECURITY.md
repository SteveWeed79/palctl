# Security Policy

palctl handles things worth protecting: your Palworld server's `AdminPassword`,
a Discord bot token, and a localhost API that can stop your server, restore a
backup over the live world, or kick and ban players. Reports about any of that
are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting instead:
**[Security → Report a vulnerability](https://github.com/SteveWeed79/pal-it-up/security/advisories/new)**.
That opens a private thread with the maintainer; nothing is public until a fix
is out.

palctl is maintained by one person, so there's no security team or SLA — but
you can expect an acknowledgement within a few days, and a fix for anything
confirmed to be prioritised ahead of everything else. You'll be credited in the
release notes unless you'd rather not be.

## Supported versions

Only the **latest release** gets security fixes. Upgrades are in-place
(run the new installer over the old one; config is preserved), so staying
current is cheap.

## The security model — what's a bug and what's by design

Knowing where palctl draws its lines makes reports (and triage) faster.

**The boundary is your user account on your machine.**

- The daemon's control API and the web dashboard bind **127.0.0.1 only** and
  every request must carry a random per-user token
  (`%APPDATA%/palctl/daemon_token`, created owner-only, compared in constant
  time). Anything that lets a request through **without** the token, lets the
  token leak to another local user, or gets the API listening on a non-loopback
  address **is a vulnerability** — please report it.
- Secrets (admin password, Discord bot token) live in **Windows Credential
  Manager** (DPAPI, encrypted to your account) — never in a config file. Any
  code path that writes a secret to disk in the clear, logs it, or includes it
  in an **Export diagnostics** bundle **is a vulnerability**.
- Backup and restore names are validated against path traversal; an operation
  escaping the backups folder or the SaveGames folder **is a vulnerability**.

**By design / out of scope:**

- Anything that requires already running code **as your user account**. A
  process running as you can read your token file and your DPAPI secrets —
  that's the Windows security model, not a palctl hole.
- The Palworld REST API itself (port 8212). Pocketpair says it must not be
  exposed to the internet; palctl only ever talks to it on 127.0.0.1 and tells
  you not to port-forward it. Deliberately exposing it is a server
  misconfiguration — though if a palctl **default** makes that mistake easy to
  stumble into, that's worth a report.
- Vulnerabilities in the Palworld server binary, SteamCMD, NSSM, or other
  third-party software palctl launches or downloads. (Ideas for *verifying*
  those downloads are welcome — see CONTRIBUTING.md.)
- The unsigned installer / SmartScreen prompt. Known tradeoff; every release
  publishes `SHA256SUMS.txt` so downloads can be verified, and free signing via
  SignPath Foundation is on the roadmap.
