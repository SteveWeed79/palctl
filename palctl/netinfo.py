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
