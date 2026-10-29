"""Read-only artifact checks; --selftest separately imports the frozen runtime."""

from __future__ import annotations

import plistlib
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcall.local_models import asset_ready, bundled_assets  # noqa: E402
from agentcall.macos_service_management import bundled_plists  # noqa: E402


def verify(bundle: Path, project: Path = ROOT) -> None:
    with (project / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    with (bundle / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    resources = bundle / "Contents/Resources"
    with (resources / "pyproject.toml").open("rb") as stream:
        embedded_version = tomllib.load(stream)["project"]["version"]
    if not (info.get("CFBundleShortVersionString") == info.get("CFBundleVersion") == embedded_version == version):
        raise ValueError("Bundle version differs from pyproject.toml")
    if info.get("LSMinimumSystemVersion") != "13.0":
        raise ValueError("Bundle must require macOS 13.0")
    for asset in bundled_assets():
        if not asset_ready(asset, resources / "models"):
            raise ValueError(f"Missing bundled model: {asset.id}")
    for label, expected in bundled_plists().items():
        with (bundle / "Contents/Library/LaunchAgents" / f"{label}.plist").open("rb") as stream:
            if plistlib.load(stream) != expected:
                raise ValueError(f"Invalid embedded agent: {label}")
    print(f"Bundle verified: version={version}, models and embedded agents complete")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
