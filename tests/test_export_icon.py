import importlib.util
from pathlib import Path

from PIL import Image


EXPORT_ICON_PATH = Path(__file__).parent.parent / "packaging" / "export-icon.py"


def _load_export_icon_module():
    module_specification = importlib.util.spec_from_file_location(
        "daisy_export_icon",
        EXPORT_ICON_PATH,
    )
    assert module_specification is not None
    assert module_specification.loader is not None
    export_icon_module = importlib.util.module_from_spec(module_specification)
    module_specification.loader.exec_module(export_icon_module)
    return export_icon_module


def test_legacy_macos_icon_is_opaque_full_canvas() -> None:
    export_icon_module = _load_export_icon_module()

    rendered_icon = export_icon_module.render_legacy_macos_icon()

    assert rendered_icon.size == (1024, 1024)
    assert rendered_icon.mode == "RGBA"
    assert rendered_icon.getchannel("A").getextrema() == (255, 255)
    assert rendered_icon.getpixel((0, 0)) == rendered_icon.getpixel((1023, 1023))
    assert rendered_icon.getpixel((512, 512)) != rendered_icon.getpixel((0, 0))


def test_macos_icon_contains_multiple_opaque_resolutions(
    tmp_path: Path,
) -> None:
    export_icon_module = _load_export_icon_module()
    generated_icon_path = tmp_path / "Daisy.icns"
    export_icon_module.MACOS_ICON = generated_icon_path

    export_icon_module.create_macos_icon()

    with Image.open(generated_icon_path) as generated_icon:
        assert generated_icon.info["sizes"] == [
            (512, 512, 2),
            (512, 512, 1),
            (256, 256, 2),
            (256, 256, 1),
            (128, 128, 2),
            (128, 128, 1),
            (32, 32, 2),
            (16, 16, 2),
        ]
        assert generated_icon.getchannel("A").getextrema() == (255, 255)
