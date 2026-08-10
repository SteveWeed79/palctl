"""Stands the world up and hands a test a handle on it.

The daemon here is the real `palctl.daemon` in a real subprocess, reached over
its real HTTP API. The only things faked are the two edges palctl cannot own —
the service manager and the Palworld server — and both are faked as *processes*,
not as patched functions, so every layer in between (subprocess handling,
timeouts, the poll loop, the event bus, state persistence) is under test.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")

from palctl.client import DAEMON_PORT  # noqa: E402
from palctl.localauth import TOKEN_HEADER  # noqa: E402

SIM_ROOT = Path(__file__).parent
BASE = f"http://127.0.0.1:{DAEMON_PORT}"

# The shim palctl actually shells out to. A file named `systemctl`, early on
# PATH, that hands the verb to fake_service.py.
_SHIM = """#!/bin/sh
exec "{python}" "{module}" "$@"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sim:
    """One simulated machine: a config dir, a fake service, a fake server, and
    the daemon that manages them."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        self.home = tmp_path / "home"          # stands in for %APPDATA%
        self.cfgdir = self.home / "palctl"
        self.bin = tmp_path / "bin"
        self.cfgdir.mkdir(parents=True, exist_ok=True)
        self.bin.mkdir(parents=True, exist_ok=True)
        self.api_port = free_port()
        self.proc: subprocess.Popen | None = None
        self.token = ""
        self.log_path = tmp_path / "daemon.out"

        shim = self.bin / "systemctl"
        shim.write_text(
            _SHIM.format(python=sys.executable, module=SIM_ROOT / "fake_service.py"),
            encoding="utf-8",
        )
        shim.chmod(0o755)

        self.service_state_file = tmp_path / "service.state"
        self.service_mode_file = tmp_path / "service.mode"
        self.palworld_mode_file = tmp_path / "palworld.mode"
        self.journal = tmp_path / "service.journal"
        self.service_state_file.write_text("inactive", encoding="utf-8")
        self.service_mode_file.write_text("ok", encoding="utf-8")
        self.palworld_mode_file.write_text("dead", encoding="utf-8")
        self.journal.write_text("", encoding="utf-8")

    # ---------------- configuration ----------------

    def write_config(self, **over) -> None:
        cfg = {
            "poll_seconds": 1,
            "api_host": "127.0.0.1",
            "api_port": self.api_port,
            "service_name": "palworld",
            "check_for_updates": False,
            "watchdog": {
                "enabled": True,
                "auto_restart_on_crash": True,
                "crash_confirm_polls": 2,
                "crash_restart_max_per_hour": 5,
                "memory_limit_mb": 999999,
            },
            "schedule": {"enabled": False},
        }
        cfg.update(over)
        (self.cfgdir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def write_daemon_state(self, **state) -> None:
        (self.cfgdir / "daemon_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    def read_daemon_state(self) -> dict:
        try:
            return json.loads(
                (self.cfgdir / "daemon_state.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}

    def env(self, **extra) -> dict:
        env = dict(os.environ)
        env["APPDATA"] = str(self.home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        # boot_daemon.py runs as a script, so its own directory — not the repo
        # root — is what lands on sys.path. Point at the checkout explicitly so
        # both launch styles import the same palctl.
        repo_root = str(SIM_ROOT.parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            [repo_root, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [repo_root]
        )
        env["SIM_DIR"] = str(self.dir)
        env["SIM_PORT"] = str(self.api_port)
        # A headless box's keyring is either absent or hostile; don't probe it.
        env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        env.update(extra)
        return env

    # ---------------- the world, driven from outside palctl ----------------

    def systemctl(self, verb: str) -> None:
        """Drive the service manager directly — this is 'somebody used
        services.msc', the thing palctl has to notice but never did."""
        subprocess.run(
            [str(self.bin / "systemctl"), verb, "palworld"],
            env=self.env(), check=False, capture_output=True, timeout=60,
        )

    def service_state(self) -> str:
        return self.service_state_file.read_text(encoding="utf-8").strip()

    def set_service_mode(self, mode: str) -> None:
        self.service_mode_file.write_text(mode, encoding="utf-8")

    def set_server_mode(self, mode: str) -> None:
        """ok | slow | hang | dead — see fake_palworld."""
        self.palworld_mode_file.write_text(mode, encoding="utf-8")

    def service_calls(self) -> list[str]:
        return [
            line.split(" ", 1)[1]
            for line in self.journal.read_text(encoding="utf-8").splitlines()
            if " " in line
        ]

    # ---------------- the daemon ----------------

    def start_daemon(self, *, pretend_boot: bool = False, **env) -> None:
        """Launch the real daemon. `pretend_boot` makes it believe the machine
        just came up — the only way to exercise the boot-intent path without
        rebooting the test runner, and equally the only way to keep it *out* of
        scenarios that aren't about it (a CI runner is often minutes old, so the
        real boot time would make that vary by runner)."""
        args = [sys.executable, str(SIM_ROOT / "boot_daemon.py")]
        env.setdefault("SIM_BOOT_AGE", "10" if pretend_boot else "21600")
        with open(self.log_path, "ab") as logf:
            self.proc = subprocess.Popen(
                args, env=self.env(**env), stdout=logf, stderr=subprocess.STDOUT
            )
        self.token = self._wait_until_up()

    def _wait_until_up(self, timeout: float = 40.0) -> str:
        token_path = self.cfgdir / "daemon_token"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"daemon exited early:\n{self.log()}")
            try:
                if httpx.get(f"{BASE}/healthz", timeout=2.0).status_code == 200:
                    token = token_path.read_text(encoding="utf-8").strip()
                    if token:
                        return token
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(0.2)
        raise RuntimeError(f"daemon never came up:\n{self.log()}")

    def shutdown(self) -> None:
        """Tear the whole simulated machine down: the daemon, then the fake
        server behind the service.

        Both halves, always. Stopping only the daemon leaves fake_palworld
        running — it is a child of the fake service manager, not of pytest — and
        a leaked one outlives the run holding a port. That is a flaky suite
        waiting to happen, and it is exactly what a stale process from an
        earlier run looked like the first time it happened here.
        """
        self.stop_daemon()
        with contextlib.suppress(Exception):
            self.systemctl("stop")

    def stop_daemon(self) -> None:
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    # ---------------- talking to the daemon ----------------

    def state(self) -> dict:
        r = httpx.get(f"{BASE}/state", headers={TOKEN_HEADER: self.token}, timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return httpx.post(
            f"{BASE}{path}",
            headers={TOKEN_HEADER: self.token},
            json=body or {},
            timeout=120,
        )

    def decisions(self) -> list[dict]:
        r = httpx.get(
            f"{BASE}/decisions", headers={TOKEN_HEADER: self.token}, timeout=15
        )
        r.raise_for_status()
        return r.json()["decisions"]

    def decision_trail(self) -> str:
        """Newest-last, for putting in an assertion message: this is the record
        of what palctl thought it was doing, which is the first thing anyone
        debugging a failure here wants."""
        return "\n".join(
            f"  {d['action']} x{d['count']}: {d['why']}"
            for d in reversed(self.decisions())
        )

    def events(self) -> list[str]:
        return [e.get("message", "") for e in self.state().get("events", [])]

    def _trail(self) -> str:
        try:
            return self.decision_trail()
        except Exception as e:  # the daemon may be gone; the log still helps
            return f"  (unavailable: {e})"

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no daemon log)"

    # ---------------- waiting ----------------

    def wait_for(self, predicate, timeout: float = 30.0, what: str = "condition"):
        """Poll until true, then return. Fails with the daemon's log attached —
        a timeout here almost always means palctl decided something, and the log
        is where it says what."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = predicate()
            except (httpx.HTTPError, OSError):
                value = None
            if value:
                return value
            time.sleep(0.3)
        raise AssertionError(
            f"timed out waiting for {what}\n"
            f"--- palctl's decisions ---\n{self._trail()}\n"
            f"--- daemon log ---\n{self.log()}"
        )

    def stays(self, predicate, seconds: float = 8.0, what: str = "condition") -> None:
        """Assert something remains true — for 'palctl leaves it alone', where
        the bug is an action that arrives a few polls later."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not predicate():
                raise AssertionError(
                    f"{what} stopped holding\n"
                    f"--- palctl's decisions ---\n{self._trail()}\n"
                    f"--- daemon log ---\n{self.log()}"
                )
            time.sleep(0.4)


@pytest.fixture
def sim(tmp_path):
    """A simulated machine with the server already up and palctl watching it —
    the state most scenarios start from. Tests that need a different starting
    point build their own via Sim(tmp_path)."""
    s = Sim(tmp_path)
    s.write_config()
    try:
        yield s
    finally:
        s.shutdown()


def running_server(s: Sim) -> None:
    """Bring the world to 'server up, daemon watching, first poll seen'."""
    s.systemctl("start")
    s.start_daemon()
    s.wait_for(lambda: s.state().get("alive") is True, what="the first successful poll")
