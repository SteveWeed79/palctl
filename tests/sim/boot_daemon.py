"""The real daemon, launched with the machine's uptime under the test's control.

`_restore_boot_intent` is deliberately restricted to real boots
(daemon.is_boot_start), so exercising it — or proving it stays out of the way —
means controlling the one input that check has. $SIM_BOOT_AGE is how long ago
the machine "booted", in seconds.

Every scenario launches through here rather than `-m palctl.daemon`, including
the ones that want a non-boot start: a CI runner is often itself minutes old, so
leaving the real boot time in place would make "this is not a boot" true or
false depending on the runner's uptime. Nothing else is changed — the daemon
below is the real one, running its real startup.
"""

import os
import sys
import time

import palctl.procs as procs

_age = float(os.environ.get("SIM_BOOT_AGE", "21600"))  # 6h: an ordinary restart
procs.boot_time = lambda: time.time() - _age

sys.argv = ["palctl-daemon", "run"]

from palctl.daemon import main  # noqa: E402

main()
