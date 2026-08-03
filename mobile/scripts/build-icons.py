"""Derive the mobile app's icons from the artwork the desktop app already uses.

Not drawn here, and not copied by hand: `web/src-tauri/Frank.icon/Assets/face.png` is the mark,
and everything below is that file placed on the backgrounds each platform wants. Two clients
carrying two hand-made copies of a logo is how they stop being the same logo — which is the same
argument the icon export in `packaging/export-icon.py` makes for the desktop's own surfaces.

The shapes differ because the platforms differ, and each difference is a requirement rather than
a preference:

  - **iOS** masks the icon into a squircle itself, so the source must be full-bleed. Handing it
    the desktop's pre-rounded tile would round an already-rounded corner and leave white slivers.
  - **Android** composites a foreground over a background and then masks *both*, so the mark has
    to sit inside the safe zone — the outer ~28% of an adaptive icon can be cropped away by the
    launcher's mask.
  - **The splash** is the mark alone on transparency, because `app.json` gives it a background.

Run with `uv run python mobile/scripts/build-icons.py` from the repository root.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MARK = REPOSITORY_ROOT / "web" / "src-tauri" / "Frank.icon" / "Assets" / "face.png"
IMAGES = REPOSITORY_ROOT / "mobile" / "assets" / "images"

# The tile blue, sampled from the desktop icon rather than guessed, so the two match.
BRAND_BLUE = (67, 143, 253, 255)

# How much of the canvas the mark occupies. On a full-bleed iOS tile it can be generous; inside
# an Android adaptive icon it cannot, because the launcher may crop the outer band away.
IOS_MARK_SCALE = 0.62
ANDROID_MARK_SCALE = 0.44


def mark(size: int) -> Image.Image:
    return Image.open(MARK).convert("RGBA").resize((size, size), Image.LANCZOS)


def centred(canvas: Image.Image, artwork: Image.Image) -> Image.Image:
    offset = ((canvas.width - artwork.width) // 2, (canvas.height - artwork.height) // 2)
    canvas.alpha_composite(artwork, offset)
    return canvas


def write(image: Image.Image, name: str) -> None:
    path = IMAGES / name
    image.save(path)
    print(f"{path.relative_to(REPOSITORY_ROOT)}  {image.width}×{image.height}")


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)

    # iOS and the general icon: full-bleed blue, the mark centred, corners left to the platform.
    for name, size in (("icon.png", 1024), ("favicon.png", 96)):
        write(centred(Image.new("RGBA", (size, size), BRAND_BLUE), mark(int(size * IOS_MARK_SCALE))), name)

    # Android's adaptive pair, plus the monochrome layer themed icons are tinted from.
    write(Image.new("RGBA", (1024, 1024), BRAND_BLUE), "android-icon-background.png")
    write(
        centred(Image.new("RGBA", (1024, 1024), (0, 0, 0, 0)), mark(int(1024 * ANDROID_MARK_SCALE))),
        "android-icon-foreground.png",
    )
    write(
        centred(Image.new("RGBA", (1024, 1024), (0, 0, 0, 0)), mark(int(1024 * ANDROID_MARK_SCALE))),
        "android-icon-monochrome.png",
    )

    # The splash, on the background `app.json` names.
    write(centred(Image.new("RGBA", (512, 512), (0, 0, 0, 0)), mark(512)), "splash-icon.png")


if __name__ == "__main__":
    main()
