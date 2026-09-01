"""
Synchronous client for the daemon's token-gated localhost API.

The CLI uses this; anything else running as your user can too. Deliberately
tiny — the daemon owns all the logic, this just speaks its localhost HTTP
dialect and turns failures into one friendly error type.
"""

from __future__ import annotations

import httpx

from . import localauth

DAEMON_PORT = 8830  # localhost only; the daemon binds 127.0.0.1


def daemon_reachable(timeout: float = 0.5) -> bool:
    """Is a daemon answering on the localhost control port? A bare TCP probe —
    no token, no HTTP — so the GUI can decide at launch whether setup actually
    produced a running daemon, cheaply and without auth noise in the logs."""
    import socket

    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", DAEMON_PORT)) == 0


class DaemonError(RuntimeError):
    """Anything that prevents talking to the daemon — with a human message."""


def _os_user() -> str:
    """The logged-in username, for labelling CLI actions. Never fails: this is
    cosmetic, and an unnamed actor is better than a crashed command."""
    import getpass

    try:
        return getpass.getuser()[:64]
    except Exception:
        return ""


class DaemonClient:
    # Long default timeout: a Stop waits for the service to actually stop,
    # which on a busy server takes a minute or two.
    def __init__(
        self,
        port: int = DAEMON_PORT,
        token: str | None = None,
        timeout: float = 180.0,
        via: str = "cli",
        actor: str = "",
    ) -> None:
        self._base = f"http://127.0.0.1:{port}"
        self._token = token if token is not None else localauth.get_or_create_token()
        self._timeout = timeout
        # Which surface is driving, and which person. The daemon records both on
        # every event the action produces, so "who stopped the server" has an
        # answer when the GUI, the dashboard, the CLI and Discord can all do it.
        # The OS username is a convenience default, not authentication — the
        # shared token already authorised the call.
        self.via = via
        self.actor = actor or _os_user()

    def _request(self, method: str, path: str, json: dict | None = None):
        try:
            r = httpx.request(
                method,
                f"{self._base}{path}",
                headers={localauth.TOKEN_HEADER: self._token},
                json=json,
                timeout=self._timeout,
            )
        except httpx.RequestError as e:
            raise DaemonError(
                f"Can't reach the palctl daemon at {self._base}. Is it running? "
                "(Start it with `palctl-daemon` or via the palctl-daemon service.)"
            ) from e

        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}

        if r.status_code == 401:
            raise DaemonError(
                "The daemon rejected the token. The CLI and the daemon must run "
                "as the same user (they share the token in the palctl config dir)."
            )
        if r.status_code >= 400:
            msg = data.get("error") if isinstance(data, dict) else None
            raise DaemonError(msg or f"Daemon returned HTTP {r.status_code}.")
        return data

    def state(self) -> dict:
        return self._request("GET", "/state")

    def backups(self) -> list[dict]:
        return self._request("GET", "/backups")

    def action(self, what: str, **body) -> dict:
        # `via`/`actor` label the surface and the person for the event feed.
        # A caller that sets them explicitly wins; otherwise this is the CLI.
        labelled = {"via": self.via, "actor": self.actor, **body}
        return self._request("POST", f"/action/{what}", json=labelled)
