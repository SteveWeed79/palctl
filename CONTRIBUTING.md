# Contributing to palctl

Thanks for wanting to help. Issues and pull requests are welcome.

## License and the CLA — read this first

palctl is licensed under **AGPL-3.0-or-later**. Contributions come in under that
same license.

palctl also uses a **Contributor License Agreement** ([CLA.md](CLA.md)). It's
short, and its only real purpose is to keep the door open for **commercial
licensing**: the AGPL fits most people, but some users (e.g. a shop that wants to
bundle palctl into a closed product) need different terms, and offering those
terms is only possible if the maintainer can relicense every contribution. The
CLA grants that right while you keep ownership of your work.

**By opening a pull request, you agree to the CLA for the contributions in it.**
Please also sign off each commit:

```
git commit -s        # adds a "Signed-off-by: Your Name <you@example.com>" line
```

For automated tracking you can wire up [cla-assistant.io](https://cla-assistant.io)
later; for now the sign-off plus the PR is the record.

## Development setup

```
pip install -e .[dev]
pytest
ruff check palctl tests
```

The test suite covers the **platform-neutral core** — the ini parser, backups,
sessions, config, scheduler, path detection, the SteamCMD argv/ini-guard, the
NSSM command builders, and the REST-API bootstrap — and runs on any OS. The
daemon's actual service control, the real network downloads, and the PySide6 GUI
need Windows and aren't in CI, so exercise those by hand on a Windows box when
you touch them.

CI runs `pytest` and `ruff` on Windows and Linux for Python 3.11 and 3.12; keep
both green.

## Style

Match the surrounding code. This codebase leans on comments that explain *why* a
thing is the way it is (the memory watchdog's guard rails, the ini round-trip's
quoted-comma handling, why the daemon and GUI are separate processes) rather than
restating *what*. New modules keep the platform-neutral logic importable and
tested on any OS, with the Windows-only pieces isolated so they fail cleanly
elsewhere.

## Good first areas

- Tests for the async orchestration (`scheduler.update_server`) with a faked
  SteamCMD.
- Exposing backup **restore** in the GUI and Discord bot (the logic already
  exists in `backups.restore`).
- A Linux server-control backend (systemd) behind the same interface as the
  Windows service layer.
