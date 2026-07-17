# Discord bot — full setup guide

The palctl Discord bot is **self-hosted**: your own application, your own token,
running inside your own daemon on your own box. There is no cloud bridge and no
subscription. This guide walks the whole thing end to end — creating the bot,
inviting it, wiring it to palctl, giving it the right permissions, and fixing the
two things that trip everyone up (the `Not allowed.` reply and notifications that
silently never arrive).

If you just want the three-line version, it's in the [README](../README.md#discord-optional).
This page is the long form.

---

## What you get

**Slash commands** (run from Discord):

| Command | What it does | Admin only? |
|---|---|---|
| `/status` | Server status, FPS, memory, uptime | no |
| `/players` | Who's online | no |
| `/playtime <name>` | Total playtime for a player | no |
| `/backups` | List recent backups | no |
| `/announce <message>` | In-game announcement | **yes** |
| `/save` | Save the world now | **yes** |
| `/backup` | Take a backup now | **yes** |
| `/restore <name>` | Restore a backup (restarts the server) | **yes** |
| `/restart [reason]` | Restart with an in-game countdown | **yes** |
| `/update` | Update via SteamCMD (stops the server) | **yes** |
| `/kick <name> [reason]` | Kick a player | **yes** |
| `/ban <name> [reason]` | Ban a player | **yes** |
| `/unban <user_id>` | Unban by user ID | **yes** |

**Automatic notifications** (the bot posts to your channel on its own):

| Event | Default | Toggle in `config.json` |
|---|---|---|
| Player join / leave | on | `notify_join_leave` |
| Level up | on | `notify_level_up` |
| Watchdog (memory) alerts | on | `notify_watchdog` |
| Server up / down | on | `notify_server_up_down` |
| Update available (server or palctl) | on | `notify_update_available` |
| Restart / backup / update / restore results | always | — |
| Errors (e.g. token rejected) | always | — |

Plus an optional **live status embed** that refreshes in place instead of
spamming (`status_message`), and a **join welcome message** with a `{name}`
placeholder (`welcome_message`).

**What it can't do:** there is **no chat relay**. Palworld exposes no chat-read
endpoint and dedicated servers ship no log file, so mirroring in-game chat into
Discord is impossible on the supported API — that's RE-UE4SS territory, not
something palctl can add. Everything above is reconstructed from polling the REST
API's `/players` and `/metrics`.

---

## Prerequisites

- A working palctl install with the daemon running and the **REST API reachable**
  (`/status` in the GUI or `palctl status` shows the server, not "unreachable").
  The bot is a thin layer over that API — if palctl itself can't see the server,
  the bot can't either.
- A Discord server (guild) where **you have the Manage Server permission** — you
  need it to invite the bot and to run admin commands under the default setup.

---

## Step 1 — Create the application and bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. **New Application** → give it a name (this is the bot's display name) → **Create**.
3. Left sidebar → **Bot**.
4. Click **Reset Token** → **Copy**. This is your **bot token** — treat it like a
   password. Anyone with it can run code *as your bot*. You'll paste it into
   palctl in Step 3. If you ever leak it, come back here and **Reset Token** to
   invalidate the old one.

> **Privileged intents:** you do **not** need to enable "Message Content Intent",
> "Server Members Intent", or "Presence Intent". palctl never reads chat and
> resolves admin roles from the slash-command interaction itself. Leave them off.

---

## Step 2 — Invite the bot to your server

Still in the portal, use the OAuth2 URL generator so the invite carries the right
scopes *and* permissions in one go:

1. Left sidebar → **OAuth2** → **URL Generator**.
2. Under **Scopes**, tick:
   - **`bot`**
   - **`applications.commands`** ← without this the slash commands never appear
3. A **Bot Permissions** box appears below. Tick:
   - **View Channels**
   - **Send Messages**
   - **Embed Links** ← every notification is an embed; without this they silently fail
4. Copy the **Generated URL** at the bottom, open it in your browser, pick your
   server, and **Authorize**.

That's the minimum. The bot needs nothing else for the default feature set —
don't grant Administrator "just in case".

> These are **server-wide** defaults. Discord lets you override permissions
> per-channel, and a channel override *beats* the server default. See
> [Step 6](#step-6--give-the-bot-permission-in-the-channel) — this is the single
> most common reason notifications don't show up.

---

## Step 3 — Give palctl the token and turn the bot on

### Windows (GUI)

1. Open the palctl GUI → **Config** tab → **Discord bot** group.
2. **Enabled**: check it.
3. **Bot token**: paste the token from Step 1. It's stored **encrypted** in
   Windows Credential Manager (DPAPI), never in a plaintext file.
4. Fill in **Channel ID** and **Admin role ID** — see Steps 4 and 5 first for how
   to get them.
5. **Save config && reload daemon**.

> **Running palctl as a Windows service?** The bot token lives in *your* user's
> Credential Manager, which the default LocalSystem service account can't read.
> Register the service with `palctl-daemon install-service --as-user` so it runs
> as you and can see the token. (Login-startup mode already runs as you — nothing
> extra needed.)

### Headless Linux (no GUI)

There's no CLI subcommand for Discord config yet, so you set it in two places by
hand.

1. **Store the token** in the keyring (needs a SecretService/DBus backend, e.g.
   `gnome-keyring` unlocked in your session):

   ```bash
   python3 -c "import keyring; keyring.set_password('palctl', 'discord_token', 'YOUR_TOKEN_HERE')"
   ```

   If the box has no keyring backend at all, the token can't be stored securely
   and the bot won't start — palctl deliberately refuses to write secrets in the
   clear. A desktop-less server usually needs `gnome-keyring` (or similar) plus
   an unlocked session for this to work.

2. **Edit `~/.config/palctl/config.json`** and set the `discord` section (see the
   full field list in [Configuration reference](#configuration-reference)):

   ```json
   "discord": {
     "enabled": true,
     "channel_id": 123456789012345678,
     "admin_role_id": 123456789012345678
   }
   ```

3. Reload: `palctl reload-config` (or restart the daemon).

---

## Step 4 — Get the channel ID

The bot posts notifications to **one** channel, identified by numeric ID.

1. In Discord: **User Settings** (gear) → **Advanced** → turn on **Developer Mode**.
2. Right-click the text channel you want the bot to post in → **Copy Channel ID**.
3. Paste it into **Channel ID** (GUI) or `channel_id` (config.json).

If this is wrong or blank, notifications go nowhere — silently. Slash commands
still work (they reply wherever you typed them), which is why a bad channel ID
looks like "commands work but alerts don't".

---

## Step 5 — Set who can run admin commands

Admin-only commands (`/announce`, `/restart`, `/kick`, `/ban`, `/save`, `/backup`,
`/restore`, `/update`, `/unban`) are gated by **`admin_role_id`**.

Despite the name, this field accepts **either a role ID or a user ID** — palctl
allows the caller if they *hold a role* with that ID **or** if they *are* the user
with that ID. Discord IDs carry no type tag (a role ID and a user ID are the same
kind of number), so palctl just checks both rather than making you get the "right
kind." Pick whichever fits:

**Option A — leave it empty (simplest).** With the field set to `0`/blank, palctl
falls back to allowing anyone with the Discord **Manage Server** permission. As the
owner, that's already you. Nothing else to do.

**Option B — a dedicated role** (best when more than one person should have access).
1. Discord → **Server Settings** → **Roles** → create a role (e.g. "Palworld Admin").
2. Assign it to yourself and anyone else who should have admin commands:
   right-click a member → **Roles** → tick it.
3. With Developer Mode on, **Server Settings → Roles → right-click the role →
   Copy Role ID**.
4. Paste it into **Admin role/user ID** / `admin_role_id`, save, reload. Grant or
   revoke access later by just adding/removing the role — no palctl change needed.

**Option C — just your own account.** Right-click your name → **Copy User ID**,
paste that in. Only that one account gets admin commands. (This is why "I added my
ID" now works — a user ID is a valid value here.)

> A typo'd ID that matches *neither* any role you hold nor your user ID simply
> denies — the safe default. If admin commands unexpectedly say `Not allowed.`,
> re-copy the ID and confirm you actually hold that role (Option B) or that it's
> your account's ID (Option C).

---

## Step 6 — Give the bot permission in the channel

Even with the invite from Step 2, a **channel-level permission override** can
block the bot, and Discord makes those overrides win over server defaults. This is
the usual cause of "the bot is online, commands work, but no join/leave alerts
ever appear" — the automatic send is refused and the failure is swallowed.

In the target channel: **Edit Channel → Permissions**. Make sure the bot (its role
or the bot itself) has, for that channel:

- ✅ **View Channel**
- ✅ **Send Messages**
- ✅ **Embed Links**

If any of these is denied at the channel level, override it back to allow for the
bot's role.

---

## Configuration reference

Full `discord` block in `%APPDATA%\palctl\config.json` (Windows) or
`~/.config/palctl/config.json` (Linux). **Only `enabled`, `channel_id`, and
`admin_role_id` are exposed in the GUI** — the rest are config-file only today,
and all default to sensible values, so you only touch them to customize.

```json
"discord": {
  "enabled": false,
  "channel_id": 0,
  "admin_role_id": 0,
  "notify_join_leave": true,
  "notify_level_up": true,
  "notify_watchdog": true,
  "notify_server_up_down": true,
  "notify_update_available": true,
  "status_message": false,
  "welcome_message": ""
}
```

| Field | Type | Meaning |
|---|---|---|
| `enabled` | bool | Master on/off for the bot. |
| `channel_id` | int | Channel the bot posts notifications to. `0` = nowhere. |
| `admin_role_id` | int | Role **or** user ID allowed to run admin commands. `0` = fall back to Manage Server. |
| `notify_join_leave` | bool | Post player join and leave alerts. |
| `notify_level_up` | bool | Post level-up alerts. |
| `notify_watchdog` | bool | Post memory-watchdog alerts (hold-off, restart, recovery). |
| `notify_server_up_down` | bool | Post 🔴 down / 🟢 up alerts. |
| `notify_update_available` | bool | Post when a server or palctl update is available. |
| `status_message` | bool | Keep one live-status embed refreshed in place instead of separate `/status` spam. |
| `welcome_message` | str | Sent to the channel when a player joins. `{name}` is replaced with the player's name. Player names are treated as untrusted — mentions can't ping `@everyone` or roles. |

The **token** is not in this file — it's in the OS keyring (Credential Manager /
SecretService), always.

Config changes are picked up on **reload** (GUI "Save && reload", `palctl
reload-config`, or a daemon restart). One exception: **changing the token of an
already-running bot needs a full daemon restart** — discord.py can't swap tokens
on a live connection.

---

## Troubleshooting

**`/announce` (or any admin command) replies `Not allowed.`**
That's palctl's admin check, not a Discord error — the command *is* reaching the
bot. The configured `admin_role_id` matches neither a role you hold nor your user
ID (see [Step 5](#step-5--set-who-can-run-admin-commands)). Fix: paste a role ID
you actually hold, or your own user ID, or blank the field to fall back to Manage
Server.

**Slash commands work, but automatic notifications never appear.**
Almost always one of these, all of which fail silently:
1. **Channel permissions** — bot lacks View Channel / Send Messages / **Embed
   Links** in that channel ([Step 6](#step-6--give-the-bot-permission-in-the-channel)).
   Embed Links is easy to miss and every alert is an embed.
2. **`channel_id`** is wrong, blank, or points at a channel the bot can't see.
3. The relevant **`notify_*` toggle** is off in `config.json`.
4. The **REST API is unreachable**, so there's nothing to report on. Check
   `/status` shows real numbers, not "unreachable".

**The slash commands don't show up when I type `/`.**
The bot was invited without the **`applications.commands`** scope. Re-invite using
the URL from [Step 2](#step-2--invite-the-bot-to-your-server) with that scope
ticked. Global command sync can also take a few minutes to propagate the first
time — give it up to an hour, or kick the daemon to force a re-sync.

**The bot shows offline / never connects.**
- Token wrong or reset: palctl posts an **error** notification "Discord token
  rejected" and stops retrying. Re-copy the token (Step 1) and re-enter it.
- Running as a **LocalSystem service** on Windows: it can't read your encrypted
  token — re-register with `--as-user` (see [Step 3](#windows-gui)).
- Network not up at boot: palctl retries the initial connect with a backoff, so a
  brief delay after a reboot is normal.

**A specific player never triggers join/leave/level-up alerts.**
palctl keys player tracking on the Palworld `userId`. On some server builds — or
in the first moments after someone connects — the REST API returns an empty
`userId`, and that player is skipped until it populates. This is a Palworld API
quirk, not a Discord one.

**Everything's set but nothing reloaded.**
Config edits need a reload; a **token** change needs a full **daemon restart**
(not just reload). When in doubt, restart the daemon.
