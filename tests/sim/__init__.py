"""A fake world for palctl to be wrong about.

Everything else in tests/ fakes at a module boundary: patch `procs.service_state`,
patch `steamcmd.run_update`, assert palctl called the right thing. That style
cannot catch the failures this package exists for, because those failures are
palctl drawing the wrong conclusion from a *real* environment that misbehaves —
a Palworld server that accepts the connection and never answers, a service
manager somebody drove behind palctl's back, an update that stalls forever.
Every one of those shipped past a green test suite.

So this package builds the environment instead of the boundary: a fake Palworld
REST API with failure modes, a fake service manager that starts and stops it for
real, and a fake SteamCMD. The daemon under test is the actual daemon, launched
as a subprocess, talking over a real socket to a real (fake) server, and driven
through its own HTTP API. Nothing is patched.

Linux-only by construction — the fake service manager is a `systemctl` on PATH,
which is what palctl shells out to off Windows (procs._state_command).
"""
