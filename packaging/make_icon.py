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

Frame format matters: Windows only supports PNG-compressed frames at 256×256;
every smaller frame must be a classic 32-bit BMP (DIB). The 1.0.0 installer
shipped an all-PNG .ico and Explorer showed the generic-exe icon whenever a
view asked for a sub-256 frame (e.g. the Downloads folder) while 256px
contexts looked fine — so this writes DIB frames below 256 and PNG at 256.

Re-run this only when ``app-icon.svg`` or the tile design changes:

    python packaging/make_icon.py

Needs ``cairosvg`` (which pulls in Pillow, used to re-encode the small frames)
and its libcairo system library (Linux/macOS build hosts have it; it is not
required at palctl runtime or in CI).
"""

from __future__ import annotations

import io
import re
import struct
from pathlib import Path

import cairosvg
from PIL import Image

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


def _dib_frame(png: bytes) -> bytes:
    """Re-encode a PNG frame as the classic 32-bit BMP (DIB) icon image.

    BITMAPINFOHEADER with doubled height, then the bottom-up BGRA colour (XOR)
    plane, then the 1-bit AND mask (rows padded to 32 bits; bit set where the
    pixel is fully transparent — modern Windows keys on the alpha channel, but
    the mask keeps legacy renderers from drawing a black box).
    """
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = img.size
    rgba = img.tobytes()

    xor = bytearray()
    for y in range(h - 1, -1, -1):  # DIB rows run bottom-up
        row = bytearray(rgba[y * w * 4 : (y + 1) * w * 4])
        row[0::4], row[2::4] = row[2::4], row[0::4]  # RGBA -> BGRA
        xor += row

    stride = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(stride)
        for x in range(w):
            if rgba[(y * w + x) * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits

    header = struct.pack(
        "<IiiHHIIiiII",
        40, w, h * 2,           # biSize, biWidth, biHeight (XOR + AND planes)
        1, 32, 0,               # biPlanes, biBitCount, biCompression (BI_RGB)
        len(xor) + len(mask),   # biSizeImage
        0, 0, 0, 0,
    )
    return header + xor + mask


def _write_ico(frames: dict[int, bytes], out: Path) -> None:
    """Assemble the .ico (read by Windows, PyInstaller, Inno Setup).

    ICONDIR header, one ICONDIRENTRY per size, then the image blobs. Windows
    only reads PNG-compressed frames at 256×256 (where a width/height byte of
    0 means 256, per the ICO spec); every smaller frame must be a classic DIB
    or Explorer falls back to the generic icon in sub-256 views.
    """
    entries = sorted(frames.items())
    offset = 6 + 16 * len(entries)
    directory = struct.pack("<HHH", 0, 1, len(entries))  # reserved, type=icon, count
    blobs = b""
    for size, png in entries:
        image = png if size >= 256 else _dib_frame(png)
        directory += struct.pack(
            "<BBBBHHII",
            size & 0xFF,  # width  (0 => 256)
            size & 0xFF,  # height (0 => 256)
            0,            # palette colours (0 = none / truecolour)
            0,            # reserved
            1,            # colour planes
            32,           # bits per pixel
            len(image),
            offset,
        )
        blobs += image
        offset += len(image)
    out.write_bytes(directory + blobs)


def main() -> None:
    TILE_SVG.write_text(_composed_svg() + "\n", encoding="utf-8")
    print(f"wrote {TILE_SVG.relative_to(ROOT)}")
    frames = _png_frames_from_file(TILE_SVG)
    _write_ico(frames, OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
