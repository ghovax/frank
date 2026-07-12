"""Export the Icon Composer document into Tauri's platform icon formats."""

import subprocess
import tempfile
from pathlib import Path

from PIL import Image


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPOSITORY_ROOT / "web"
COMPOSER_DOCUMENT = WEB_ROOT / "src-tauri" / "Daisy.icon"
TAURI_MASTER = WEB_ROOT / "src-tauri" / "app-icon.png"
ICON_COMPOSER_TOOL = Path(
    "/Applications/Icon Composer.app/Contents/Executables/ictool"
)
MASTER_SIZE = 2048
LEGACY_MACOS_FOOTPRINT = 1.0


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
    composer_export = Image.open(composer_export_path).convert("RGBA")
    footprint_size = round(MASTER_SIZE * LEGACY_MACOS_FOOTPRINT)
    legacy_rendition = composer_export.resize(
        (footprint_size, footprint_size),
        Image.Resampling.LANCZOS,
    )
    tauri_master = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    inset = (MASTER_SIZE - footprint_size) // 2
    tauri_master.alpha_composite(legacy_rendition, (inset, inset))
    tauri_master.save(TAURI_MASTER, optimize=True)


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
    if not ICON_COMPOSER_TOOL.is_file():
        raise FileNotFoundError(f"Icon Composer tool not found: {ICON_COMPOSER_TOOL}")
    if not COMPOSER_DOCUMENT.is_dir():
        raise FileNotFoundError(f"Icon Composer document not found: {COMPOSER_DOCUMENT}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        composer_export_path = Path(temporary_directory) / "composer-export.png"
        export_composer_document(composer_export_path)
        create_tauri_master(composer_export_path)

    generate_tauri_icons()
    print(f"Exported {COMPOSER_DOCUMENT} to {TAURI_MASTER} and Tauri icon assets")


if __name__ == "__main__":
    main()
