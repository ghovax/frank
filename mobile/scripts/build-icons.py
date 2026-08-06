"""Derive the mobile app's icons from the artwork the desktop app already uses."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MARK = REPOSITORY_ROOT / "web" / "src-tauri" / "LangMesh.icon" / "Assets" / "face.png"
IMAGES = REPOSITORY_ROOT / "mobile" / "assets" / "images"

# The tile blue, sampled from the desktop icon rather than guessed, so the two match.
BRAND_BLUE = (67, 143, 253, 255)

# How much of the canvas the mark occupies, which an adaptive icon's crop limits.
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
