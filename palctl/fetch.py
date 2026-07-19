"""
HTTPS downloads that survive a broken certificate environment.

The frozen Windows build downloads three things over HTTPS (the WinSW service
wrapper, the VC++ runtime, SteamCMD). Python verifies those connections against
the system certificate store — and on a surprising number of real boxes that
verification fails (`CERTIFICATE_VERIFY_FAILED: unable to get local issuer`):
an antivirus doing HTTPS scanning with an interception cert Python can't chain,
a stripped-down cert store, or a server that sends an incomplete chain (Python
doesn't do AIA fetching; browsers do, which is why "it works in Chrome").

So: try the system trust first, and on a *verification* failure retry once
against the CA bundle `certifi` ships (already installed — httpx depends on
it). If both fail, raise with the actual story and what to do about it, instead
of the bare `_ssl.c:1010` that stops setup dead. Verification is never
disabled — a failure still fails closed.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request


def open_url(url: str, timeout: float):
    """urlopen with a certifi fallback on certificate-verification failure.
    Returns the response object (caller uses it as a context manager)."""
    try:
        return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
    # System trust couldn't verify — retry against certifi's bundled CAs.
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        return urllib.request.urlopen(url, timeout=timeout, context=ctx)  # noqa: S310
    except (ImportError, urllib.error.URLError) as e2:
        reason = getattr(e2, "reason", e2)
        raise OSError(
            f"could not verify the HTTPS connection to {url} ({reason}). This is "
            "usually an antivirus or proxy doing HTTPS scanning, or a machine "
            "with a broken certificate store — palctl will not download over a "
            "connection it can't verify."
        ) from e2
