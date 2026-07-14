"""
Generate ``packaging/app-icon.ico`` from ``palctl/gui/icons/app-icon.svg``.

The in-app icons are transparent, single-colour stroke glyphs that inherit the
Qt palette (see ``palctl/gui/icons.py``). A *Windows* app icon can't do that —
it shows on the taskbar, the installer, and Start-Menu shortcuts over
backgrounds we don't control — so this composes the same shield-and-pulse glyph
onto a rounded green tile (white glyph on brand green reads on both light and
dark taskbars).

It writes two committed assets from one source:

* ``palctl/gui/icons/app-icon-tile.svg`` — the composed tile, rendered as-is by
  the GUI for the window/taskbar icon so it matches the installed .ico exactly.
* ``packaging/app-icon.ico`` — a multi-resolution icon for the frozen exes and
  the Inno Setup installer, so a Windows build needs no rasteriser.

Re-run this only when ``app-icon.svg`` or the tile design changes:

    python packaging/make_icon.py

Needs ``cairosvg`` and its libcairo system library (Linux/macOS build hosts have
it; it is not required at palctl runtime or in CI).
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import cairosvg

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "palctl" / "gui" / "icons"
SVG = ICON_DIR / "app-icon.svg"
TILE_SVG = ICON_DIR / "app-icon-tile.svg"
OUT = Path(__file__).resolve().parent / "app-icon.ico"

# Windows shows icons at many sizes; render each natively (not by downscaling one
# big raster) so the thin stroke stays crisp down to 16px.
SIZES = (16, 24, 32, 48, 64, 128, 256)

# Brand green tile (top-lit gradient) with a white glyph — the app's "healthy"
# colour, and legible on light and dark taskbars alike.
TILE_TOP = "#56d364"
TILE_BOTTOM = "#2ea043"
GLYPH = "#ffffff"


def _glyph_paths(svg_text: str) -> str:
    """Pull the raw <path .../> elements out of the source app-icon so a future
    edit to the glyph flows through to the .ico automatically."""
    paths = re.findall(r"<path\b[^>]*/>", svg_text)
    if not paths:
        raise SystemExit(f"no <path> elements found in {SVG}")
    return "\n    ".join(paths)


def _composed_svg() -> str:
    glyph = _glyph_paths(SVG.read_text(encoding="utf-8"))
    # 24×24 space (same as the glyph). Full-bleed rounded tile, glyph scaled to
    # ~0.72 around centre for padding; stroke bumped so it stays bold once scaled.
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{TILE_TOP}"/>
      <stop offset="1" stop-color="{TILE_BOTTOM}"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="24" height="24" rx="5.2" fill="url(#tile)"/>
  <g fill="none" stroke="{GLYPH}" stroke-width="2.05" stroke-linecap="round"
     stroke-linejoin="round" transform="translate(12 12) scale(0.72) translate(-12 -12)">
    {glyph}
  </g>
</svg>"""


def _png_frames_from_file(path: Path) -> dict[int, bytes]:
    return {
        size: cairosvg.svg2png(url=str(path), output_width=size, output_height=size)
        for size in SIZES
    }


def _write_ico(frames: dict[int, bytes], out: Path) -> None:
    """Assemble a PNG-framed .ico (Vista+; read by Windows, PyInstaller, Inno).

    ICONDIR header, one ICONDIRENTRY per size, then the PNG blobs. A width/height
    byte of 0 means 256 per the ICO spec.
    """
    entries = sorted(frames.items())
    offset = 6 + 16 * len(entries)
    directory = struct.pack("<HHH", 0, 1, len(entries))  # reserved, type=icon, count
    blobs = b""
    for size, png in entries:
        directory += struct.pack(
            "<BBBBHHII",
            size & 0xFF,  # width  (0 => 256)
            size & 0xFF,  # height (0 => 256)
            0,            # palette colours (0 = none / truecolour)
            0,            # reserved
            1,            # colour planes
            32,           # bits per pixel
            len(png),
            offset,
        )
        blobs += png
        offset += len(png)
    out.write_bytes(directory + blobs)


def main() -> None:
    TILE_SVG.write_text(_composed_svg() + "\n", encoding="utf-8")
    print(f"wrote {TILE_SVG.relative_to(ROOT)}")
    frames = _png_frames_from_file(TILE_SVG)
    _write_ico(frames, OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
