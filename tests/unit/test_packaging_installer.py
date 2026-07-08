"""Static checks for macOS standalone installer build script."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = PROJECT_ROOT / "packaging" / "build_installer.sh"
PACKAGING_README = PROJECT_ROOT / "packaging" / "README.md"


def test_build_installer_script_exists_and_builds_app_and_dmg():
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "PyInstaller" in text
    assert "CallPilot.app" in text
    assert "CallPilot.dmg" in text
    assert "hdiutil create" in text
    assert "AGENTCALL_FFMPEG_PATH" in text
    assert "AGENTCALL_LIBUSB_PATH" in text
    assert "CODESIGN_IDENTITY" in text
    assert "NOTARY_PROFILE" in text


def test_packaging_files_have_no_local_user_paths():
    for path in (BUILD_SCRIPT, PACKAGING_README, PROJECT_ROOT / "packaging" / "agentcall.spec"):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text
        assert "C:\\Users\\" not in text


def test_packaging_readme_mentions_unsigned_first_open():
    text = PACKAGING_README.read_text(encoding="utf-8")

    assert "right-click" in text
    assert "Open" in text
    assert "Application Support/CallPilot" in text
    assert "LaunchAgents" in text
