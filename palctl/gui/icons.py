"""
Runtime icon loading for the GUI.

The icon set (``palctl/gui/icons/*.svg``) is one visual language of transparent,
single-colour stroke glyphs drawn with ``stroke="currentColor"``. ``QIcon``
won't recolour an SVG on its own, so we render each SVG to a ``QPixmap`` and tint
it — by default to the current palette's text colour, so glyphs track the active
light/dark theme and match the label they sit beside.

Two exceptions render with their own colours instead of being tinted:

* status / tray glyphs carry a *semantic* colour (green ok, amber warning, red
  error) so state reads at a glance on any tray background;
* the window/app icon uses the composed brand tile (``app-icon-tile.svg``), so
  the title-bar/taskbar icon matches the installed Windows ``.ico`` exactly.

Import is defensive: if ``QtSvg`` isn't present (it ships with PySide6, but a
stripped build could drop it) or an asset is missing, every loader degrades to
an empty ``QIcon`` and the GUI still runs — just without that glyph.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

try:
    from PySide6.QtSvg import QSvgRenderer

    _HAVE_SVG = True
except Exception:  # pragma: no cover - QtSvg absent only in a stripped build
    _HAVE_SVG = False

ICON_DIR = Path(__file__).with_name("icons")

# Semantic colours, matched to the accent colours already used in main.py
# (#3fb950 green, #d29922 amber, #f85149 red). Chosen to read on both light and
# dark backgrounds, including the Windows notification-area tray.
OK = "#3fb950"
WARN = "#d29922"
FAIL = "#f85149"

# Pixmap sizes baked into multi-resolution icons (tray, window) so Qt has a crisp
# raster at each size the OS may ask for.
_TRAY_SIZES = (16, 24, 32, 48)
_APP_SIZES = (16, 24, 32, 48, 64, 128, 256)

_TRAY_STATES = {
    "idle": ("tray-idle", OK),
    "warning": ("tray-warning", WARN),
    "error": ("tray-error", FAIL),
}


def _svg_path(name: str) -> Path:
    return ICON_DIR / f"{name}.svg"


def _resolve_color(color: str | QColor | None) -> str:
    """A concrete #rrggbb string — an explicit colour, else the palette's text
    colour, else a light default when there's no QApplication yet."""
    if color is not None:
        return QColor(color).name()
    app = QApplication.instance()
    if app is not None:
        return app.palette().color(QPalette.ColorRole.WindowText).name()
    return "#e6e9ef"


@lru_cache(maxsize=512)
def _tinted_pixmap(name: str, color_hex: str, size: int) -> QPixmap:
    """The SVG rendered at ``size`` then flooded with ``color_hex`` through its
    own alpha, so the tint fully determines the colour (the source renders black
    from ``currentColor``, which we overwrite)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    path = _svg_path(name)
    if not _HAVE_SVG or not path.exists():
        return pm
    renderer = QSvgRenderer(str(path))
    painter = QPainter(pm)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pm.rect(), QColor(color_hex))
    painter.end()
    return pm


@lru_cache(maxsize=64)
def _plain_pixmap(name: str, size: int) -> QPixmap:
    """The SVG rendered at ``size`` keeping its own fills — for full-colour
    assets like the brand tile."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    path = _svg_path(name)
    if not _HAVE_SVG or not path.exists():
        return pm
    renderer = QSvgRenderer(str(path))
    painter = QPainter(pm)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pm


def load_icon(name: str, *, color: str | QColor | None = None, size: int = 64) -> QIcon:
    """A tinted icon for a tab, button, or menu action. ``color`` defaults to the
    palette text colour; ``size`` is the base raster Qt scales from."""
    return QIcon(_tinted_pixmap(name, _resolve_color(color), size))


def tray_icon(state: str) -> QIcon:
    """The system-tray icon for a server/watchdog ``state`` — one of ``idle``,
    ``warning``, ``error`` (anything else falls back to ``idle``). Semantically
    coloured and multi-resolution so it's sharp at tray sizes."""
    name, color = _TRAY_STATES.get(state, _TRAY_STATES["idle"])
    icon = QIcon()
    for s in _TRAY_SIZES:
        icon.addPixmap(_tinted_pixmap(name, color, s))
    return icon


def app_icon() -> QIcon:
    """The window / taskbar icon. Prefers the full-colour brand tile (matching
    the Windows .ico); falls back to a green-tinted shield if the tile asset
    isn't present."""
    icon = QIcon()
    if _svg_path("app-icon-tile").exists():
        for s in _APP_SIZES:
            icon.addPixmap(_plain_pixmap("app-icon-tile", s))
    else:
        for s in _APP_SIZES:
            icon.addPixmap(_tinted_pixmap("app-icon", OK, s))
    return icon
