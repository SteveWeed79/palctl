"""
Open the Windows Firewall for the web dashboard when LAN access is enabled.

Binding the daemon to ``0.0.0.0`` (``ui_bind_host``) makes it *listen* on every
interface, but on Windows the firewall still drops inbound connections to the
dashboard port by default — so the LAN toggle would be a silent no-op without
this. When LAN access is on, palctl adds an inbound TCP allow rule scoped to the
**Private** (and Domain) profiles — never Public, so carrying the server box to
an untrusted network doesn't expose it. When LAN access is turned off again, the
rule is removed, so the port doesn't stay open.

Best-effort: editing the firewall needs an elevated process. The daemon usually
is one (a LocalSystem service), but a login-startup daemon runs as the plain
user; a non-elevated attempt fails cleanly and the caller logs the single
``netsh`` command to run by hand.

Windows-only. The command builders are pure and unit-tested anywhere; the
runners no-op (return ``"skipped"``) off Windows, where a ``0.0.0.0`` bind
reaches the LAN without any palctl-managed firewall rule anyway.
"""

from __future__ import annotations

import subprocess
import sys

# Stable rule name so ensure/remove/show all refer to the same rule.
RULE_NAME = "palctl dashboard"
# Private = home/work networks; Domain = a corporate-joined box. Never Public.
_PROFILES = "private,domain"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------- pure command builders ----------------


def add_rule_command(port: int, *, name: str = RULE_NAME) -> list[str]:
    return [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={name}", "dir=in", "action=allow", "protocol=TCP",
        f"localport={port}", f"profile={_PROFILES}",
        "description=palctl web dashboard (LAN access)",
    ]


def remove_rule_command(*, name: str = RULE_NAME) -> list[str]:
    return ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"]


def show_rule_command(*, name: str = RULE_NAME) -> list[str]:
    return ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"]


def manual_add_command(port: int, *, name: str = RULE_NAME) -> str:
    """The copy-pasteable elevated command, logged when palctl can't add the
    rule itself (the daemon isn't elevated)."""
    return (
        f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow '
        f"protocol=TCP localport={port} profile={_PROFILES}"
    )


# ---------------- runners (Windows) ----------------


def _on_windows() -> bool:
    return sys.platform.startswith("win")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)


def rule_present(*, name: str = RULE_NAME) -> bool:
    """Whether a dashboard firewall rule already exists. False off Windows."""
    if not _on_windows():
        return False
    try:
        # `show rule` exits 0 when a match exists, 1 ("No rules match…") when not.
        return _run(show_rule_command(name=name)).returncode == 0
    except OSError:
        return False


def ensure_rule(port: int, *, name: str = RULE_NAME) -> str:
    """Add the inbound allow rule if it isn't already there. Returns one of:
    ``'skipped'`` (not Windows), ``'present'`` (already exists), ``'added'``
    (created now), or ``'failed'`` (netsh refused — usually: not elevated)."""
    if not _on_windows():
        return "skipped"
    if rule_present(name=name):
        return "present"
    try:
        return "added" if _run(add_rule_command(port, name=name)).returncode == 0 else "failed"
    except OSError:
        return "failed"


def remove_rule(*, name: str = RULE_NAME) -> str:
    """Remove any dashboard rule. Returns ``'skipped'`` (not Windows),
    ``'absent'`` (nothing to remove), ``'removed'``, or ``'failed'``."""
    if not _on_windows():
        return "skipped"
    if not rule_present(name=name):
        return "absent"
    try:
        return "removed" if _run(remove_rule_command(name=name)).returncode == 0 else "failed"
    except OSError:
        return "failed"
