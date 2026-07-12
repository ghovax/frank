"""Build the Daisy app icon with Apple's own tools.

The only compositing done here is the unavoidable minimum: a full-bleed background and the
🌼 emoji glyph (rendered by Core Text, the native text renderer) centred on top. No squircle
is drawn and no "glass" is faked — the .icns is then assembled by Apple's `iconutil` from a
set of sizes produced by Apple's `sips`, which is the sanctioned no-Xcode pipeline. On modern
macOS the system applies the rounded-rectangle mask to the full-bleed art itself.

    uv run python packaging/make-icon.py
    # -> writes web/src-tauri/icons/{icon.icns, 32x32.png, 128x128.png, 128x128@2x.png}
"""
import io
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import AppKit
from PIL import Image

CANVAS = 1024
COVERAGE = 0.78  # flower size relative to the full canvas (leaves an even margin)
ICONS = Path("web/src-tauri/icons")
BACKGROUND_TOP = (238, 243, 253)
BACKGROUND_BOTTOM = (206, 219, 243)


def render_emoji(char: str, point_size: float) -> Image.Image:
    font = AppKit.NSFont.fontWithName_size_("Apple Color Emoji", point_size)
    attributes = {AppKit.NSFontAttributeName: font}
    string = AppKit.NSString.stringWithString_(char)
    measured = string.sizeWithAttributes_(attributes)
    width, height = int(math.ceil(measured.width)) + 6, int(math.ceil(measured.height)) + 6
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    AppKit.NSColor.clearColor().set()
    AppKit.NSBezierPath.fillRect_(((0, 0), (width, height)))
    string.drawAtPoint_withAttributes_((3, 3), attributes)
    AppKit.NSGraphicsContext.restoreGraphicsState()
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        column.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return column.resize((size, size)).convert("RGBA")


# Full-bleed master: a soft cool background with the flower centred on it.
master = vertical_gradient(CANVAS, BACKGROUND_TOP, BACKGROUND_BOTTOM)
flower = render_emoji("🌼", 720)
bounds = flower.getbbox()
if bounds:
    flower = flower.crop(bounds)
scale = (CANVAS * COVERAGE) / max(flower.size)
flower = flower.resize((round(flower.width * scale), round(flower.height * scale)), Image.LANCZOS)
master.alpha_composite(flower, ((CANVAS - flower.width) // 2, (CANVAS - flower.height) // 2))

ICONS.mkdir(parents=True, exist_ok=True)
master_path = ICONS / "source_icon.png"
master.save(master_path)

# Assemble the .icns with Apple's tools: sips renders each required size into an .iconset,
# iconutil packs it. This is the official pipeline — no third-party rasterizer.
with tempfile.TemporaryDirectory() as work:
    iconset = Path(work) / "Daisy.iconset"
    iconset.mkdir()
    specs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]
    for base, scale_factor in specs:
        pixels = base * scale_factor
        name = f"icon_{base}x{base}{'@2x' if scale_factor == 2 else ''}.png"
        subprocess.run(["sips", "-z", str(pixels), str(pixels), str(master_path),
                        "--out", str(iconset / name)], check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICONS / "icon.icns")], check=True)

# The extra PNG sizes Tauri lists alongside the .icns, resized by sips from the same master.
for name, pixels in (("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256)):
    subprocess.run(["sips", "-z", str(pixels), str(pixels), str(master_path),
                    "--out", str(ICONS / name)], check=True, capture_output=True)

master.resize((256, 256), Image.LANCZOS).save("/tmp/icon_preview.png")
print("wrote", ICONS / "icon.icns", "+ png sizes (sips + iconutil)")
