"""Convert the approved source image into PyInstaller macOS and Windows icons."""

from pathlib import Path

from PIL import Image


# Windows and macOS consume different icon containers from the same approved image.
ICON_SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)


def main() -> None:
    """Create deterministic ICO and ICNS assets from the tracked source PNG."""

    project_root = Path(__file__).resolve().parents[1]
    icon_directory = project_root / "assets" / "icons"
    source_path = icon_directory / "source.png"
    # RGBA avoids macOS icon conversion failures for source assets without alpha.
    source_image = Image.open(source_path).convert("RGBA")
    icon_images = [source_image.resize((size, size)) for size in ICON_SIZES]
    icon_images[-1].save(
        icon_directory / "douyin-compass.ico",
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES[:-1]],
    )
    icon_images[-1].save(
        icon_directory / "douyin-compass.icns",
        format="ICNS",
        append_images=icon_images[:-1],
    )


if __name__ == "__main__":
    main()
