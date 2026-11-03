"""Rebuild the Windows multi-size icon from the tracked macOS source (Pillow)."""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    with Image.open(ROOT / "packaging" / "CallPilot.icns") as source:
        source.convert("RGBA").save(
            ROOT / "packaging" / "CallPilot.ico", format="ICO",
            sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)],
        )


if __name__ == "__main__":
    main()
