"""Generate the macOS ICNS and export the remaining Tauri icon formats."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPOSITORY_ROOT / "web"
COMPOSER_DOCUMENT = WEB_ROOT / "src-tauri" / "LangMesh.icon"
TAURI_MASTER = WEB_ROOT / "src-tauri" / "app-icon.png"
MACOS_ICON = WEB_ROOT / "src-tauri" / "icons" / "icon.icns"
ICON_COMPOSER_TOOL = Path(
    "/Applications/Icon Composer.app/Contents/Executables/ictool"
)
MACOS_CANVAS_SIZE = 1024
MACOS_ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# The two surfaces the icon tool does not know about, derived here so they cannot fall behind.
MONO_ARTWORK = COMPOSER_DOCUMENT / "Assets" / "face-mono.png"
TRAY_ICON = WEB_ROOT / "src-tauri" / "icons" / "tray-icon.png"
TRAY_ICON_SIZE = 88
# Browser-tab icons: the mark alone on transparency, since a tab strip already frames what it shows.
TAB_ICONS = (
    (WEB_ROOT / "src" / "app" / "icon.png", 512),
    (WEB_ROOT / "src" / "app" / "favicon.ico", 256),
)
# The iOS home-screen icon keeps its tile, because iOS fills transparency with black.
APPLE_TOUCH_ICON = (WEB_ROOT / "src" / "app" / "apple-icon.png", 512)
WEB_CORNER_RADIUS_RATIO = 0.22


def export_composer_document(output_path: Path) -> None:
    subprocess.run(
        [
            ICON_COMPOSER_TOOL,
            COMPOSER_DOCUMENT,
            "--export-image",
            "--output-file",
            output_path,
            "--platform",
            "macOS",
            "--rendition",
            "Default",
            "--width",
            "1024",
            "--height",
            "1024",
            "--scale",
            "2",
        ],
        check=True,
    )


def create_tauri_master(composer_export_path: Path) -> None:
    """Keep Icon Composer's rendered artwork for non-macOS platform assets."""
    with Image.open(composer_export_path) as exported_image:
        composer_export = exported_image.convert("RGBA")
    normalized_export = composer_export.resize(
        composer_export.size,
        Image.Resampling.LANCZOS,
    )
    normalized_export.save(TAURI_MASTER, optimize=True)


def _extended_srgb_fill(fill_description: str) -> tuple[int, int, int, int]:
    """Convert Icon Composer's extended-sRGB fill into eight-bit channels."""
    color_space, separator, component_description = fill_description.partition(":")
    if color_space != "extended-srgb" or not separator:
        raise ValueError(f"Unsupported icon fill: {fill_description}")

    color_components = component_description.split(",")
    if len(color_components) != 4:
        raise ValueError(f"Unsupported icon fill: {fill_description}")

    red_channel, green_channel, blue_channel, alpha_channel = (
        round(float(color_component) * 255)
        for color_component in color_components
    )
    return red_channel, green_channel, blue_channel, alpha_channel


def render_tiled_icon() -> Image.Image:
    """Render unmasked full-canvas artwork for macOS to shape exactly once."""
    configuration = json.loads((COMPOSER_DOCUMENT / "icon.json").read_text())
    fill_description = configuration["fill"]["automatic-gradient"]
    background = Image.new(
        "RGBA",
        (MACOS_CANVAS_SIZE, MACOS_CANVAS_SIZE),
        _extended_srgb_fill(fill_description),
    )

    for group_configuration in configuration["groups"]:
        for layer_configuration in group_configuration["layers"]:
            artwork_path = (
                COMPOSER_DOCUMENT
                / "Assets"
                / layer_configuration["image-name"]
            )
            with Image.open(artwork_path) as artwork_image:
                artwork = artwork_image.convert("RGBA")
            position = layer_configuration.get("position", {})
            scale = float(position.get("scale", 1.0))
            if scale <= 0:
                raise ValueError(
                    f"Icon layer scale must be positive: {layer_configuration['name']}"
                )
            scaled_size = (
                round(artwork.width * scale),
                round(artwork.height * scale),
            )
            scaled_artwork = artwork.resize(
                scaled_size,
                Image.Resampling.LANCZOS,
            )
            translation = position.get("translation-in-points", (0, 0))
            if len(translation) != 2:
                raise ValueError(
                    "Icon layer translation must contain horizontal and vertical values: "
                    f"{layer_configuration['name']}"
                )
            destination = (
                round((MACOS_CANVAS_SIZE - scaled_artwork.width) / 2 + translation[0]),
                round((MACOS_CANVAS_SIZE - scaled_artwork.height) / 2 + translation[1]),
            )
            background.alpha_composite(scaled_artwork, destination)

    return background


def create_macos_icon() -> None:
    """Write the raw macOS artwork at every resolution in the ICNS container."""
    source_image = render_tiled_icon()
    resized_images = [
        source_image.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
        for icon_size in MACOS_ICON_SIZES[:-1]
    ]
    MACOS_ICON.parent.mkdir(parents=True, exist_ok=True)
    source_image.save(
        MACOS_ICON,
        format="ICNS",
        append_images=resized_images,
    )


def create_tray_icon() -> None:
    """Write the menu-bar icon as a template image: pure black plus alpha, which macOS tints itself."""
    with Image.open(MONO_ARTWORK) as artwork_image:
        alpha_channel = artwork_image.convert("RGBA").getchannel("A")
    tray_icon = Image.new("RGBA", alpha_channel.size, (0, 0, 0, 0))
    tray_icon.putalpha(alpha_channel)
    tray_icon.resize((TRAY_ICON_SIZE, TRAY_ICON_SIZE), Image.Resampling.LANCZOS).save(
        TRAY_ICON, optimize=True
    )


def _rounded_mask(size: int) -> Image.Image:
    """A squircle-ish corner mask, approximated because these surfaces show it at 32 pixels or less."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=round(size * WEB_CORNER_RADIUS_RATIO), fill=255
    )
    return mask


def create_web_icons() -> None:
    """Write the browser favicons from the same artwork as everything else."""
    fill_configuration = json.loads((COMPOSER_DOCUMENT / "icon.json").read_text())
    brand_red, brand_green, brand_blue, _ = _extended_srgb_fill(
        fill_configuration["fill"]["automatic-gradient"]
    )
    with Image.open(MONO_ARTWORK) as artwork_image:
        alpha_channel = artwork_image.convert("RGBA").getchannel("A")
    mark = Image.new("RGBA", alpha_channel.size, (brand_red, brand_green, brand_blue, 0))
    mark.putalpha(alpha_channel)
    for destination, size in TAB_ICONS:
        destination.parent.mkdir(parents=True, exist_ok=True)
        mark.resize((size, size), Image.Resampling.LANCZOS).save(destination)

    destination, size = APPLE_TOUCH_ICON
    tile = render_tiled_icon().resize((size, size), Image.Resampling.LANCZOS)
    tile.putalpha(_rounded_mask(size))
    tile.save(destination)


def generate_tauri_icons() -> None:
    subprocess.run(
        [
            "nix",
            "develop",
            str(REPOSITORY_ROOT),
            "--command",
            "cargo",
            "tauri",
            "icon",
            "src-tauri/app-icon.png",
        ],
        cwd=WEB_ROOT,
        check=True,
    )


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument(
        "--macos-only",
        action="store_true",
        help="Generate only the raw multi-resolution macOS ICNS file.",
    )
    arguments = argument_parser.parse_args()

    if not COMPOSER_DOCUMENT.is_dir():
        raise FileNotFoundError(f"Icon Composer document not found: {COMPOSER_DOCUMENT}")
    if arguments.macos_only:
        create_macos_icon()
        print(f"Generated {MACOS_ICON} from {COMPOSER_DOCUMENT}")
        return
    if not MONO_ARTWORK.is_file():
        raise FileNotFoundError(f"Monochrome artwork not found: {MONO_ARTWORK}")
    if not ICON_COMPOSER_TOOL.is_file():
        raise FileNotFoundError(f"Icon Composer tool not found: {ICON_COMPOSER_TOOL}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        composer_export_path = Path(temporary_directory) / "composer-export.png"
        export_composer_document(composer_export_path)
        create_tauri_master(composer_export_path)

    generate_tauri_icons()
    create_macos_icon()
    create_tray_icon()
    create_web_icons()
    print(
        f"Exported {COMPOSER_DOCUMENT} to {TAURI_MASTER}, the Tauri icon assets, "
        f"{TRAY_ICON.name} and the browser favicons"
    )


if __name__ == "__main__":
    main()
