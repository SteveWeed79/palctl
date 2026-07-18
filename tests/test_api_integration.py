"""End-to-end test of the daemon's localhost HTTP API — the contract the GUI,
CLI, and web dashboard all depend on. Every other test fakes at the module
boundary; this one boots the real daemon as a subprocess (no Palworld server
needed — service control just reads UNKNOWN, the REST API just reads "not
answering") and drives the endpoints over the wire, which is exactly where the
1.0.0 verification caught real bugs (e.g. a FileNotFoundError -> 500 on /state
on a systemd-less box).

Skips cleanly where the daemon's own deps aren't installed."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")   # the daemon's API server
pytest.importorskip("discord")   # imported by palctl.bot at module level
httpx = pytest.importorskip("httpx")

from palctl.client import DAEMON_PORT  # noqa: E402
from palctl.localauth import TOKEN_HEADER  # noqa: E402

BASE = f"http://127.0.0.1:{DAEMON_PORT}"


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no daemon log)"


def _wait_until_up(proc: subprocess.Popen, token_path: Path, log_path: Path) -> str:
    """Block until the daemon answers on its port and has written its token, or
    fail loudly with the daemon's own log if it dies or never comes up."""
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"daemon exited early (code {proc.returncode}):\n{_read_log(log_path)}"
            )
        try:
            r = httpx.get(f"{BASE}/", timeout=2.0)
            if r.status_code == 200 and token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
                if token:
                    return token
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"daemon never came up:\n{_read_log(log_path)}")


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    """Boot the real daemon against a throwaway config dir. Yields a namespace
    with `.token` and `.home` (the config dir), so clients like the CLI can talk
    to the same daemon."""
    home = tmp_path_factory.mktemp("palctl-home")
    env = dict(os.environ)
    # config_dir() honours APPDATA on every OS (falling back to ~/.config), so
    # this redirects the whole per-user config dir — token, config, logs — into
    # the temp dir instead of clobbering the developer's real one.
    env["APPDATA"] = str(home)
    # A headless CI box often has no usable system keyring; the null backend
    # keeps the daemon from probing one at all.
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"

    log_path = home / "daemon.out"
    token_path = home / "palctl" / "daemon_token"
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "palctl.daemon", "run"],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
        )
    try:
        token = _wait_until_up(proc, token_path, log_path)
        yield types.SimpleNamespace(token=token, home=home)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _auth(d) -> dict:
    return {TOKEN_HEADER: d.token}


def test_state_requires_the_token(daemon):
    r = httpx.get(f"{BASE}/state", timeout=5)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_state_with_token_returns_the_expected_shape(daemon):
    r = httpx.get(f"{BASE}/state", headers=_auth(daemon), timeout=10)
    assert r.status_code == 200
    body = r.json()
    # The keys the GUI/CLI/dashboard read off /state.
    for key in ("service", "alive", "players", "history", "events", "metrics"):
        assert key in body, f"/state missing {key!r}"
    assert isinstance(body["players"], list)
    assert isinstance(body["history"], list)


def test_dashboard_index_is_public_and_html(daemon):
    r = httpx.get(f"{BASE}/", timeout=5)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_favicon_is_served_without_a_token(daemon):
    # Regression for the old 401-on-every-visit console noise (#32).
    r = httpx.get(f"{BASE}/favicon.ico", timeout=5)
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers.get("content-type", "")


def test_healthz_is_public_and_reports_liveness(daemon):
    # Liveness probe for an external monitor: no token, small JSON, 200 while the
    # daemon is up (the server being down is not a daemon-health failure).
    r = httpx.get(f"{BASE}/healthz", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "last_poll_age_seconds" in body


def test_logs_requires_token_and_returns_text(daemon):
    assert httpx.get(f"{BASE}/logs", timeout=5).status_code == 401
    r = httpx.get(f"{BASE}/logs?n=50", headers=_auth(daemon), timeout=5)
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    assert "daemon up" in r.text  # the startup line the daemon logs


def test_unknown_action_is_a_400_not_a_500(daemon):
    r = httpx.post(f"{BASE}/action/nope", headers=_auth(daemon), json={}, timeout=5)
    assert r.status_code == 400
    assert "unknown action" in r.json()["error"]


def test_missing_body_field_is_a_400_with_the_field_name(daemon):
    # Regression for the old bare-KeyError 500 (#32): the client author should
    # get a 4xx that names what was missing.
    r = httpx.post(f"{BASE}/action/unban", headers=_auth(daemon), json={}, timeout=5)
    assert r.status_code == 400
    assert r.json() == {"error": "missing required field: user_id"}


def test_malformed_json_body_is_a_400(daemon):
    r = httpx.post(
        f"{BASE}/action/unban",
        headers={**_auth(daemon), "content-type": "application/json"},
        content=b"not json",
        timeout=5,
    )
    assert r.status_code == 400


def test_cli_status_drives_the_same_daemon(daemon):
    # The CLI is one of the three clients of this API; prove it authenticates
    # against the running daemon and renders a status without error.
    env = dict(os.environ)
    env["APPDATA"] = str(daemon.home)  # same config dir -> same token
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    out = subprocess.run(
        [sys.executable, "-m", "palctl.cli", "status"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip()  # printed something
