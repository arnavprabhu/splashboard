"""Draws the app icon, packaging/AppIcon.icns: the accent square with the "S" mark, the same mark
as the web favicon and the menu bar item. Run it after changing the mark; the .icns is committed,
so builds need neither this script nor its dependencies.

    uv run --no-project --with fonttools --with brotli --with pillow \\
        python packaging/scripts/make-icon.py

The "S" is Archivo, the self-hosted web font, at the display style: weight 900, width 72%.
macOS masks the full-bleed square into its own icon shape.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
FONT = REPO / "web" / "src" / "assets" / "fonts" / "archivo-latin.woff2"
OUT = REPO / "packaging" / "AppIcon.icns"
ACCENT = (211, 51, 24)  # #d33318, the favicon's accent
INK = (255, 255, 255)
SIZE = 1024
# iconutil's names: (points, scale) for each size macOS asks for.
ICONSET = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]


def display_font(size: int) -> ImageFont.FreeTypeFont:
    font = TTFont(str(FONT))
    instantiateVariableFont(font, {"wght": 900, "wdth": 72}, inplace=True)
    font.flavor = None
    data = io.BytesIO()
    font.save(data)
    data.seek(0)
    return ImageFont.truetype(data, size)


def draw() -> Image.Image:
    image = Image.new("RGB", (SIZE, SIZE), ACCENT)
    canvas = ImageDraw.Draw(image)
    font = display_font(860)
    # Center the glyph's ink, not its advance box, so the S sits optically centered.
    left, top, right, bottom = canvas.textbbox((0, 0), "S", font=font)
    x = (SIZE - (right - left)) / 2 - left
    y = (SIZE - (bottom - top)) / 2 - top
    canvas.text((x, y), "S", font=font, fill=INK)
    return image


def main() -> None:
    master = draw()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for points, scale in ICONSET:
            pixels = points * scale
            name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
            master.resize((pixels, pixels), Image.Resampling.LANCZOS).save(iconset / name)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
        preview = REPO / "build" / "AppIcon-preview.png"
        preview.parent.mkdir(exist_ok=True)
        master.save(preview)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB) and {preview}")
    if shutil.which("sips"):
        subprocess.run(["sips", "-g", "pixelWidth", str(OUT)], check=False, capture_output=True)


if __name__ == "__main__":
    main()
