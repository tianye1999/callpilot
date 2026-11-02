#!/usr/bin/env python3
"""Run isolated XCTest cases on macOS without launching the iOS app or Keychain."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".validation" / "core-tests"
BUNDLE = BUILD / "CoreTests.app" / "Contents"
BIN = BUNDLE / "MacOS"
RESOURCES = BUNDLE / "Resources"


def run(*args):
    subprocess.run(args, check=True, cwd=ROOT, encoding="utf-8")


def main():
    BIN.mkdir(parents=True, exist_ok=True)
    RESOURCES.mkdir(parents=True, exist_ok=True)
    developer = subprocess.check_output(["xcode-select", "-p"], encoding="utf-8").strip()
    frameworks = developer + "/Platforms/MacOSX.platform/Developer/Library/Frameworks"
    libraries = developer + "/Platforms/MacOSX.platform/Developer/usr/lib"
    shared = str(Path(developer).parent / "SharedFrameworks")
    sources = [
        "Protocol/Models.swift", "Protocol/ContentModels.swift", "Protocol/CallContentModels.swift",
        "Protocol/HostedCloudClient.swift", "Protocol/VoipPushPayload.swift", "Protocol/CallSignaling.swift",
        "Support/Localization.swift", "Support/AppLinks.swift", "Support/ErrorCopy.swift",
        "Call/CallState.swift", "Call/CallStatusReducer.swift", "Call/CallKitCallRegistry.swift",
        "Call/CallAudioOwner.swift", "Call/CallKitAnswerController.swift",
        "Call/CallMediaTransport.swift", "Call/CallMediaSession.swift", "Call/CallTiming.swift",
        "Content/SettingsState.swift", "UI/RootPresentation.swift",
        "Content/CallHistoryModel.swift", "Content/CallHistoryCache.swift",
        "Content/MessageInboxModel.swift", "Content/MessageCache.swift",
        "Pairing/PairingLink.swift",
        "Protocol/AlertPushRoute.swift", "Support/PushEnvironment.swift",
    ]
    tests = [
        "PairingLinkTests", "LocalizationTests", "CallKitConfigurationTests", "HostedCloudClientTests",
        "ModelsTests", "VoipPushPayloadTests", "CallSignalingTests", "CallStateTests",
        "CallStatusReducerTests", "CallKitCallRegistryTests", "SettingsStateTests",
        "RootPresentationTests", "ContentModelsTests", "CallContentModelsTests", "PrivacyManifestTests",
        "CallHistoryModelTests", "MessageInboxModelTests",
        "AlertPushTests",
        "CallAudioOwnerTests", "CallKitAnswerControllerTests", "CallMediaSessionTests", "CallTimingTests",
        "CallFirstFrameRendererRemovalTests",
    ]
    for language in ["en", "zh-Hans"]:
        destination = RESOURCES / (language + ".lproj")
        destination.mkdir(exist_ok=True)
        for name in ["Localizable.strings", "InfoPlist.strings"]:
            source = ROOT / "CallPilot/Resources" / (language + ".lproj") / name
            (destination / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (BUNDLE / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        '<key>CFBundleExecutable</key><string>CoreTests</string>'
        '<key>CFBundleIdentifier</key><string>ai.bondings.callpilot.core-tests</string>'
        '<key>CFBundleDevelopmentRegion</key><string>en</string></dict></plist>', encoding="utf-8"
    )
    flags = ["-swift-version", "6", "-F", frameworks, "-I", libraries, "-L", libraries,
             "-Xlinker", "-rpath", "-Xlinker", frameworks,
             "-Xlinker", "-rpath", "-Xlinker", libraries,
             "-Xlinker", "-rpath", "-Xlinker", shared]
    if os.environ.get("CORE_TEST_CONFIGURATION", "Debug") == "Debug":
        flags += ["-D", "DEBUG"]
    run("xcrun", "swiftc", *flags, "-emit-library", "-emit-module", "-enable-testing",
        "-module-name", "CallPilot", "-emit-module-path", str(BUILD / "CallPilot.swiftmodule"),
        "-o", str(BIN / "libCallPilot.dylib"),
        *[str(ROOT / "CallPilot" / source) for source in sources])
    runner = BUILD / "main.swift"
    runner.write_text(
        "import XCTest\nimport Darwin\nlet suite = XCTestSuite(name: \"Core tests\")\n"
        + "\n".join(f"suite.addTest({test}.defaultTestSuite)" for test in tests)
        + '\nsuite.run()\nlet result = suite.testRun!\n'
        + 'print("CORE TESTS: \\(result.executionCount) executed, \\(result.totalFailureCount) failures")\n'
        + "exit(result.hasSucceeded ? 0 : 1)\n", encoding="utf-8"
    )
    run("xcrun", "swiftc", *flags, "-I", str(BUILD), "-L", str(BIN), "-lCallPilot",
        "-Xlinker", "-rpath", "-Xlinker", "@executable_path", "-o", str(BIN / "CoreTests"),
        str(runner), str(ROOT / "CallPilotTests/ContentTestFixtures.swift"),
        *[str(ROOT / "CallPilotTests" / (test + ".swift")) for test in tests])
    run(str(BIN / "CoreTests"))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
