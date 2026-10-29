"""Static checks for macOS standalone installer build script."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = PROJECT_ROOT / "packaging" / "build_installer.sh"
PACKAGING_README = PROJECT_ROOT / "packaging" / "README.md"
SPEC_FILE = PROJECT_ROOT / "packaging" / "agentcall.spec"
ENTITLEMENTS_FILE = PROJECT_ROOT / "packaging" / "entitlements.plist"


def test_build_installer_script_exists_and_builds_app_and_dmg():
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "PyInstaller" in text
    assert "CallPilot.app" in text
    assert "CallPilot-$VERSION.dmg" in text
    assert "hdiutil create" in text
    assert "AGENTCALL_FFMPEG_PATH" in text
    assert "AGENTCALL_LIBUSB_PATH" in text
    assert "CODESIGN_IDENTITY" in text
    assert "NOTARY_PROFILE" in text
    assert '"$APP_PATH/Contents/MacOS/CallPilot" --selftest' in text
    assert "verify_bundle.py" in text
    assert 'stapler validate "$APP_PATH"' in text
    assert 'stapler validate "$DMG_PATH"' in text
    assert "RELEASE=1 requires signing and notarization" in text
    assert "dmg-settings.py" in text


def test_packaging_files_have_no_local_user_paths():
    for path in (BUILD_SCRIPT, PACKAGING_README, SPEC_FILE, ENTITLEMENTS_FILE):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text
        assert "C:\\Users\\" not in text


def test_packaging_readme_mentions_unsigned_first_open():
    text = PACKAGING_README.read_text(encoding="utf-8")

    assert "right-click" in text
    assert "Open" in text
    assert "Application Support/CallPilot" in text
    assert "LaunchAgents" in text


def test_packaged_app_declares_microphone_permission_and_seed_data():
    text = SPEC_FILE.read_text(encoding="utf-8")

    assert '"NSMicrophoneUsageDescription"' in text
    assert "CallPilot 需要采集通话对方的语音" in text
    # 版本号单一出处：spec 必须从 pyproject.toml 读版本，不得再写死字面量。
    assert '"CFBundleShortVersionString": APP_VERSION' in text
    assert 'tomllib.load(_pyproject)["project"]["version"]' in text
    assert '"number_profiles.example.json"' in text
    assert '"seed"' in text


def test_packaged_app_collects_both_livekit_namespace_distributions():
    text = SPEC_FILE.read_text(encoding="utf-8")

    assert '("livekit.rtc", "livekit.api")' in text
    assert 'copy_metadata("livekit")' in text
    assert 'copy_metadata("livekit-api")' in text
    assert '"livekit.rtc"' in (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")


def test_build_installer_codesigns_app_with_audio_input_entitlement_only():
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    entitlements = ENTITLEMENTS_FILE.read_text(encoding="utf-8")

    assert 'com.apple.security.device.audio-input' in entitlements
    assert '--entitlements "$ROOT/packaging/entitlements.plist"' in text
    app_sign = 'codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP_PATH"'
    assert app_sign not in text
    dmg_sign = 'codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG_PATH"'
    assert dmg_sign in text


@pytest.fixture
def bundle(tmp_path):
    import plistlib
    import runpy
    import tomllib

    from agentcall.local_models import bundled_assets
    from agentcall.macos_service_management import bundled_plists

    app = tmp_path / "CallPilot.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    project = (PROJECT_ROOT / "pyproject.toml").read_bytes()
    (resources / "pyproject.toml").write_bytes(project)
    version = tomllib.loads(project.decode())["project"]["version"]
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": version, "CFBundleVersion": version, "LSMinimumSystemVersion": "13.0"}))
    for asset in bundled_assets():
        for relative in asset.required_files:
            path = resources / "models" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test-model")
        for relative in asset.required_dirs:
            (resources / "models" / relative).mkdir(parents=True, exist_ok=True)
    agents = app / "Contents/Library/LaunchAgents"
    agents.mkdir(parents=True)
    for label, data in bundled_plists().items():
        (agents / f"{label}.plist").write_bytes(plistlib.dumps(data))
    verify = runpy.run_path(str(PROJECT_ROOT / "packaging/verify_bundle.py"))["verify"]
    return app, verify


def test_bundle_verifier_accepts_complete_artifact(bundle):
    app, verify = bundle
    verify(app)


def test_bundle_verifier_rejects_version_drift(bundle):
    app, verify = bundle
    (app / "Contents/Resources/pyproject.toml").write_text('[project]\nversion="0.0.0"\n')
    with pytest.raises(ValueError, match="version"):
        verify(app)


def test_bundle_verifier_rejects_missing_model(bundle):
    app, verify = bundle
    (app / "Contents/Resources/models/silero_vad.onnx").unlink()
    with pytest.raises(ValueError, match="Missing bundled model"):
        verify(app)


def test_bundle_verifier_rejects_changed_agent(bundle):
    import plistlib

    app, verify = bundle
    (app / "Contents/Library/LaunchAgents/com.agentcall.app.plist").write_bytes(plistlib.dumps({}))
    with pytest.raises(ValueError, match="Invalid embedded agent"):
        verify(app)
