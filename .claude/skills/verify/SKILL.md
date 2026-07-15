---
name: verify
description: Run palctl end-to-end on a headless Linux box and observe its real surfaces (daemon API, web dashboard, CLI).
---

# Verifying palctl changes at runtime (headless Linux)

The daemon, control API, web dashboard, and CLI all run on Linux — no
Palworld server needed (it just reads as "REST API not answering").

```bash
# 1. Start the daemon (background). The null keyring backend avoids
#    crashes on boxes with a broken/absent system keyring.
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring python -m palctl.daemon run
# log line to wait for:  "daemon up; localhost API on 127.0.0.1:8830"

# 2. Token lives at ~/.config/palctl/daemon_token (0600, created on boot).
TOKEN=$(cat ~/.config/palctl/daemon_token)
curl -H "X-Palctl-Token: $TOKEN" http://127.0.0.1:8830/state   # 200 JSON
curl http://127.0.0.1:8830/state                               # 401
curl http://127.0.0.1:8830/                                    # dashboard, 200

# 3. Dashboard in real Chromium (Playwright preinstalled at /opt/pw-browsers):
#    open http://127.0.0.1:8830/#$TOKEN — assert: no pageerror events,
#    location.hash stripped, sessionStorage['palctl-token'] set, #tiles visible.

# 4. CLI drives the same daemon:
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring python -m palctl.cli status
```

Gotchas:
- GUI (PySide6) does not import here — container lacks libEGL; CI covers it.
- `/favicon.ico` 401s by design (token gate) — harmless console noise.
- Anything calling service control reads "UNKNOWN" (no systemd in container).
- A real `stop`/`start` action blocks up to 120 s waiting on service state —
  don't drive it live here.
- Don't `pkill -f palctl` from the agent shell — the pattern matches your own
  shell's command line. Kill the background task, or match the PID from the
  daemon log.
