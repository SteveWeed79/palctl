# Vendored third-party code

## palworld_save_tools

| | |
|---|---|
| Upstream | https://github.com/cheahjs/palworld-save-tools |
| Version | **0.24.0** |
| License | MIT — see `palworld_save_tools/LICENSE` (© 2024 Jun Siang Cheah) |
| Source | PyPI sdist `palworld_save_tools-0.24.0.tar.gz` |
| sdist SHA-256 | `b9cc9cdd8aae20eb172476112ab09a7e8219da60fe6e77ee535401f61091d640` |
| Vendored | the `palworld_save_tools/` package only — the sdist is 57 MB, of which 56.9 MB is test fixtures |

### Why vendored rather than a dependency

The Windows installer ships a frozen build with no Python and no pip, so a
`pip install` extra would leave the majority of palctl's users unable to use
the save tooling at all — which is exactly the group most likely to hit save
bloat, since they are the ones running a long-lived server from the installer.

It vendors cleanly: upstream's stated policy is that "default usage of the
library must not rely on any external dependencies", so this pulls in nothing
else. 228 KB of pure Python, no build step, no native code.

Upstream also asks consumers to pin an exact version, because the JSON shape
changes across releases and round-trips are lossy between generations. A
vendored copy *is* that pin.

### Rules for this directory

- **Do not edit these files.** Nothing here is patched. If a fix is needed,
  send it upstream and re-vendor; a local patch turns every future update into
  an archaeology exercise.
- **palctl code must not import it directly.** Everything goes through
  `palctl/savescan.py`, which runs the parse in a *separate short-lived
  process*. Parsing a multi-gigabyte `Level.sav` needs multiple gigabytes of
  RAM and can be killed by the OS; that must never take the daemon — and the
  server it is supervising — down with it.
- The parse is read-only today. Writing a modified save back is a separate
  decision that has to be gated behind a verified backup.

### Updating

1. `pip download palworld-save-tools==<version> --no-deps --no-binary :all:`
2. Replace `palworld_save_tools/`, keeping `LICENSE`.
3. Update the version and SHA-256 above.
4. Run `pytest tests/test_savescan.py` — it round-trips a real generated save
   through the vendored code, so a breaking upstream change fails there rather
   than in front of a user with a 4 GB world.
