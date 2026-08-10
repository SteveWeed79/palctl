"""A stand-in service manager, so palctl's real service-control path drives a
real process.

Invoked as `systemctl <verb> <name>` through a shim on PATH — palctl shells out
to exactly that off Windows (procs._state_command / _action_command), so nothing
in palctl knows it isn't systemd. Only the three verbs palctl uses are
implemented: is-active, start, stop.

Starting the "service" launches fake_palworld.py for real; stopping it kills
that process. That is what makes decisions observable end to end: palctl decides
to start the server, and a server actually appears on the port.

The service itself has failure modes too (`service.mode`), because "palctl asked
the service manager to start it and it didn't come up" is its own class of bug
and reads very differently from a crash:

  ok           — start works.
  refuse_start — start is accepted and the service stays inactive forever, the
                 way a service with a broken logon or a missing exe behaves.
  slow_start   — start takes long enough that a caller polling for RUNNING has
                 to actually wait.

State lives in files under $SIM_DIR so the test process, the shim, and palctl
all see the same world.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

SIM_DIR = Path(os.environ["SIM_DIR"])
STATE = SIM_DIR / "service.state"
SERVICE_MODE = SIM_DIR / "service.mode"
PALWORLD_MODE = SIM_DIR / "palworld.mode"
PIDFILE = SIM_DIR / "palworld.pid"
LOG = SIM_DIR / "palworld.out"
JOURNAL = SIM_DIR / "service.journal"


def _read(path: Path, default: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or default
    except OSError:
        return default


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _note(line: str) -> None:
    """Append to a journal the test can read back, so a failed assertion can
    show what the service manager was asked to do and in what order."""
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(f"{time.time():.3f} {line}\n")


def _kill_server() -> None:
    try:
        pid = int(_read(PIDFILE, "0"))
    except ValueError:
        pid = 0
    if pid:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    PIDFILE.unlink(missing_ok=True)


def start() -> None:
    _note("start")
    mode = _read(SERVICE_MODE, "ok")
    if mode == "refuse_start":
        _write(STATE, "failed")
        return
    _write(STATE, "activating")
    if mode == "slow_start":
        time.sleep(6.0)
    _write(PALWORLD_MODE, "ok")
    with LOG.open("a", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("fake_palworld.py")),
                os.environ["SIM_PORT"],
                str(PALWORLD_MODE),
            ],
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    _write(PIDFILE, str(proc.pid))
    time.sleep(0.6)  # let it bind before we claim it's up
    _write(STATE, "active")


def stop() -> None:
    _note("stop")
    _write(STATE, "deactivating")
    _kill_server()
    _write(PALWORLD_MODE, "dead")
    time.sleep(0.2)
    _write(STATE, "inactive")


def main() -> int:
    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    if verb == "is-active":
        print(_read(STATE, "inactive"))
    elif verb == "start":
        start()
    elif verb == "stop":
        stop()
    elif verb == "status":
        print(f"state: {_read(STATE, 'inactive')}")
    return 0


sys.exit(main())
