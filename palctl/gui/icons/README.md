# palctl icon set

24 icons for the palctl GUI, tray, and diagnostics — one visual language, so the
Dashboard/Players/Console/Settings/Config tabs, the tray, and every action button
look like they belong to the same app.

Open `preview.html` in a browser to see all 24 at once.

## Style spec

- 24×24 viewBox, `stroke="currentColor"`, `fill="none"`, round caps/joins.
- Stroke width 1.75 for UI icons, 2.25 for the three tray icons (bolder so they
  hold up at 16px).
- A few icons use small solid fills (`fill="currentColor"`) for tiny details —
  the play triangle, stop square, and status dots — everything else is pure
  stroke.
- Because they use `currentColor`, each icon inherits whatever CSS/Qt palette
  color is applied to it — no separate light/dark or accent-color variants
  needed.

## What's in the set

| File | Use |
|---|---|
| `app-icon.svg` | Window icon, shortcuts, installer |
| `tab-dashboard.svg` | Dashboard tab |
| `tab-players.svg` | Players tab |
| `tab-console.svg` | Console tab |
| `tab-settings.svg` | Settings (ini editor) tab |
| `tab-config.svg` | Config tab |
| `tray-idle.svg` / `tray-warning.svg` / `tray-error.svg` | System tray, swapped based on server/watchdog state |
| `action-start.svg` / `action-stop.svg` / `action-restart.svg` | Server lifecycle buttons |
| `action-save.svg` | Save (world save) |
| `action-backup.svg` / `action-restore.svg` | Backup / restore a backup |
| `action-update.svg` | SteamCMD server update |
| `action-kick.svg` / `action-ban.svg` | Player moderation |
| `action-announce.svg` | In-game announce |
| `wizard.svg` | First-run setup wizard |
| `status-ok.svg` / `status-fail.svg` / `status-warn.svg` | Preflight check results (replaces the ✓ / ❌ / ⚠️ emoji currently in `preflight.py`) |
| `export-diagnostics.svg` | Export diagnostics button |

## Using these in the PySide6 GUI

These are wired up by [`palctl/gui/icons.py`](../icons.py) — you don't call the
SVGs directly. `QIcon` doesn't recolor SVGs on its own, so that module renders
each SVG to a tinted `QPixmap`:

```python
from palctl.gui import icons

tabs.addTab(self.dash, icons.load_icon("tab-dashboard"), "Dashboard")
```

`load_icon(name)` defaults its tint to the current palette's text colour, so
glyphs track the active light/dark theme and match the label beside them. The
status/tray glyphs are the exception: they render in a fixed semantic colour
(`icons.OK` green, `icons.WARN` amber, `icons.FAIL` red) so state reads at a
glance. `icons.tray_icon("idle" | "warning" | "error")` returns the tray icon
for a given server/watchdog state, and `icons.app_icon()` returns the
window/taskbar icon.

For the Windows app icon (`.ico` for the installer/exe), the glyph is composed
onto a rounded brand tile — see `app-icon-tile.svg` (rendered as-is for the
window icon) and `packaging/make_icon.py`, which regenerates both that tile and
`packaging/app-icon.ico` from `app-icon.svg`. Re-run it only when the app glyph
or tile design changes:

```
python packaging/make_icon.py
```

## Editing

Every file is hand-authored, plain SVG — no build step, no external icon
library dependency. Stroke width and color are the only two things you're
likely to want to tweak; both are top-of-file attributes on the `<svg>` tag.

The one exception is `app-icon-tile.svg`: it's *generated* by
`packaging/make_icon.py` from `app-icon.svg` — don't hand-edit it. Edit
`app-icon.svg` and re-run the script.
