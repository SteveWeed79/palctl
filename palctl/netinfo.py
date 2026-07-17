"""
Connect info for the "tell your friends" step.

Getting a server *running* is only half of "it works" — people still have to be
able to join it. This surfaces the two addresses players use (LAN and internet)
and the port that has to be forwarded, so the wizard can spell it out instead of
leaving a non-technical host to discover port-forwarding the hard way.
"""

from __future__ import annotations

import socket
import urllib.request

# Palworld's default game port (PublicPort in the ini). UDP.
GAME_PORT_DEFAULT = 8211

# Hosts that only accept connections from the box itself, and the "all
# interfaces" wildcards. A wildcard bind is reachable both locally (via
# loopback) and from the LAN, so it needs the box's real address to share.
_LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1"}
_WILDCARD_HOSTS = {"0.0.0.0", "::"}


def is_loopback(host: str) -> bool:
    """True when a daemon bound to `host` can only be reached from this PC."""
    return host.strip().lower() in _LOOPBACK_HOSTS


def dashboard_targets(
    host: str, port: int, token: str, lan_ip: str | None = None
) -> tuple[str, str | None]:
    """Work out which dashboard URLs to show, given what the daemon is bound to.

    Returns ``(open_url, shareable_url)``:
      open_url      — open THIS in a browser on the server box. A wildcard bind
                      ("0.0.0.0") isn't itself connectable, so we dial loopback.
      shareable_url — a URL another device on the LAN can use, or None when the
                      daemon is loopback-only (nothing off-box can reach it) or
                      the LAN address couldn't be determined.

    Pure (the caller passes lan_ip), so the URL logic is testable offline.
    """
    h = host.strip().lower()
    loopback = h in _LOOPBACK_HOSTS
    wildcard = h in _WILDCARD_HOSTS

    open_host = "127.0.0.1" if (loopback or wildcard) else host
    open_url = f"http://{open_host}:{port}/#{token}"
    if loopback:
        return open_url, None

    share_host = lan_ip if wildcard else host
    shareable_url = f"http://{share_host}:{port}/#{token}" if share_host else None
    return open_url, shareable_url


def lan_ip() -> str | None:
    """
    The address other machines on the same network use to reach this box.

    The UDP 'connect' doesn't send anything — it just makes the OS pick the
    outbound interface, whose local address is the LAN IP.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def public_ip(timeout: float = 4.0) -> str | None:
    """Best-effort public IP via a couple of echo services. None if offline."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                ip = resp.read().decode("utf-8", "replace").strip()
            if ip:
                return ip
        except Exception:
            continue
    return None
