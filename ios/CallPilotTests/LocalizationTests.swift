import Foundation
import XCTest
@testable import CallPilot

final class LocalizationTests: XCTestCase {
    func testCallMediaPrivacyPurposeStringsCoverCameraAndMicrophoneInBothLanguages() throws {
        let root = repositoryRoot
        let infoURL = root.appendingPathComponent("ios/CallPilot/Info.plist")
        let info = try XCTUnwrap(
            PropertyListSerialization.propertyList(
                from: Data(contentsOf: infoURL),
                options: [],
                format: nil
            ) as? [String: Any]
        )

        for key in ["NSCameraUsageDescription", "NSMicrophoneUsageDescription"] {
            let fallback = try XCTUnwrap(info[key] as? String, "Missing Info.plist key \(key)")
            XCTAssertFalse(fallback.isEmpty, "Empty Info.plist value for \(key)")

            for language in ["zh-Hans", "en"] {
                let url = root.appendingPathComponent(
                    "ios/CallPilot/Resources/\(language).lproj/InfoPlist.strings"
                )
                let localized = try XCTUnwrap(
                    PropertyListSerialization.propertyList(
                        from: Data(contentsOf: url),
                        options: [],
                        format: nil
                    ) as? [String: String]
                )
                let value = try XCTUnwrap(
                    localized[key],
                    "Missing \(language) purpose string for \(key)"
                )
                XCTAssertFalse(value.isEmpty, "Empty \(language) purpose string for \(key)")
            }
        }
    }

    func testM3ThroughM6StringsHaveChineseAndEnglishCatalogValues() throws {
        let root = repositoryRoot
        let referencedKeys = try localizationKeys(in: localizationSourceURLs(root: root))
        let localized = try Dictionary(uniqueKeysWithValues: ["zh-Hans", "en"].map { language in
            let url = root.appendingPathComponent(
                "ios/CallPilot/Resources/\(language).lproj/Localizable.strings"
            )
            let values = try XCTUnwrap(
                PropertyListSerialization.propertyList(
                    from: Data(contentsOf: url),
                    options: [],
                    format: nil
                ) as? [String: String]
            )
            return (language, values)
        })

        XCTAssertFalse(referencedKeys.isEmpty)
        for key in referencedKeys.sorted() {
            for language in ["zh-Hans", "en"] {
                let value = try XCTUnwrap(
                    localized[language]?[key],
                    "Missing \(language) localization for \(key)"
                )
                XCTAssertFalse(value.isEmpty, "Empty \(language) localization for \(key)")
            }
        }
    }

    func testLocalizationLookupFollowsRequestedLocale() {
        let chinese = Locale(identifier: "zh-Hans")
        let english = Locale(identifier: "en")
        XCTAssertEqual(L10n.text("tab.messages", locale: chinese), "短信")
        XCTAssertEqual(L10n.text("tab.messages", locale: english), "Messages")
        XCTAssertEqual(
            String(
                format: L10n.text("calls.duration.minutes_seconds", locale: english),
                locale: english,
                arguments: [Int64(2), Int64(3)]
            ),
            "2 min 3 sec"
        )
        XCTAssertEqual(
            String(
                format: L10n.text("calls.duration.minutes_seconds", locale: chinese),
                locale: chinese,
                arguments: [Int64(2), Int64(3)]
            ),
            "2 分 3 秒"
        )
    }

    func testAllUserFacingSourcesDoNotKeepHardCodedChineseStringLiterals() throws {
        let regex = try NSRegularExpression(
            pattern: #"\"(?:[^\"\\]|\\.)*[\u4E00-\u9FFF](?:[^\"\\]|\\.)*\""#
        )
        var findings: [String] = []
        for url in localizationSourceURLs(root: repositoryRoot) {
            let source = try String(contentsOf: url, encoding: .utf8)
            for (offset, line) in source.split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
                let code = line.split(separator: "//", maxSplits: 1, omittingEmptySubsequences: false)[0]
                let candidate = String(code)
                let range = NSRange(candidate.startIndex..., in: candidate)
                for match in regex.matches(in: candidate, range: range) {
                    guard let swiftRange = Range(match.range, in: candidate) else { continue }
                    findings.append(
                        "\(url.lastPathComponent):\(offset + 1): \(candidate[swiftRange])"
                    )
                }
            }
        }

        XCTAssertTrue(
            findings.isEmpty,
            "User-facing strings must use L10n: \(findings.joined(separator: ", "))"
        )
    }

    func testPublicSupportLinksUseStableHTTPSPages() {
        XCTAssertEqual(AppLinks.privacyPolicy.absoluteString, "https://tianye1999.github.io/callpilot/privacy.html")
        XCTAssertEqual(AppLinks.support.absoluteString, "https://tianye1999.github.io/callpilot/support.html")
        XCTAssertEqual(AppLinks.terms.absoluteString, "https://tianye1999.github.io/callpilot/terms.html")
        for url in [AppLinks.privacyPolicy, AppLinks.support, AppLinks.terms] {
            XCTAssertEqual(url.scheme, "https")
            XCTAssertNotNil(url.host)
        }
    }

    func testPairingScreenExposesPrivacyAndSupportBeforeCredentialsExist() throws {
        let source = try String(
            contentsOf: repositoryRoot.appendingPathComponent("ios/CallPilot/UI/PairView.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(source.contains("AppLinks.privacyPolicy"))
        XCTAssertTrue(source.contains("AppLinks.support"))
    }

    func testCameraPurposeStringsDoNotAdvertiseUnavailableVideoCalling() throws {
        let root = repositoryRoot
        let urls = [
            root.appendingPathComponent("ios/CallPilot/Info.plist"),
            root.appendingPathComponent("ios/CallPilot/Resources/en.lproj/InfoPlist.strings"),
            root.appendingPathComponent("ios/CallPilot/Resources/zh-Hans.lproj/InfoPlist.strings"),
        ]

        for url in urls {
            let values = try XCTUnwrap(
                PropertyListSerialization.propertyList(
                    from: Data(contentsOf: url),
                    options: [],
                    format: nil
                ) as? [String: Any]
            )
            let purpose = try XCTUnwrap(values["NSCameraUsageDescription"] as? String)
            XCTAssertFalse(purpose.localizedCaseInsensitiveContains("video calling"))
            XCTAssertFalse(purpose.contains("视频通话"))
        }
    }

    func testPairingErrorsUseStableLocalizedCopyInsteadOfServerMessages() {
        XCTAssertEqual(
            PairingErrorCopy.message(code: "INVALID_PAIRING", locale: Locale(identifier: "en")),
            "The pairing code is invalid or has expired. Generate a new code on your computer."
        )
        XCTAssertEqual(
            PairingErrorCopy.message(code: "DEVICE_LIMIT", locale: Locale(identifier: "zh-Hans")),
            "已达到配对设备上限，请先在电脑端撤销一台旧设备。"
        )
        XCTAssertEqual(
            PairingErrorCopy.message(code: "FUTURE_ERROR", locale: Locale(identifier: "en")),
            "Pairing is unavailable right now. Try again."
        )
    }

    func testCallFailuresUseStableLocalizedCopyInsteadOfWireReasons() {
        XCTAssertEqual(
            CallFailureCopy.message(
                code: "SIM_NOT_REGISTERED",
                locale: Locale(identifier: "en")
            ),
            "The SIM is not registered on the mobile network."
        )
        XCTAssertEqual(
            CallFailureCopy.message(
                code: "SERVICE_NUMBER_MISMATCH",
                locale: Locale(identifier: "zh-Hans")
            ),
            "当前 SIM 与该运营商客服号码不匹配。"
        )
        XCTAssertEqual(
            CallFailureCopy.message(code: nil, locale: Locale(identifier: "en")),
            "The call could not be completed."
        )
    }

    private var repositoryRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func localizationSourceURLs(root: URL) -> [URL] {
        [
            "ios/CallPilot/AppModel.swift",
            "ios/CallPilot/UI/PairView.swift",
            "ios/CallPilot/UI/DialView.swift",
            "ios/CallPilot/UI/IncomingOfferView.swift",
            "ios/CallPilot/UI/CallView.swift",
        ].map { root.appendingPathComponent($0) } + newPageSourceURLs(root: root)
    }

    private func newPageSourceURLs(root: URL) -> [URL] {
        [
            "ios/CallPilot/UI/MainTabShell.swift",
            "ios/CallPilot/UI/MessagesView.swift",
            "ios/CallPilot/UI/CallRecordsView.swift",
            "ios/CallPilot/UI/SettingsView.swift",
            "ios/CallPilot/Content/MessageInboxModel.swift",
            "ios/CallPilot/Content/CallHistoryModel.swift",
        ].map { root.appendingPathComponent($0) }
    }

    private func localizationKeys(in sourceURLs: [URL]) throws -> Set<String> {
        let regex = try NSRegularExpression(pattern: #"L10n\.(?:text|format)\(\"([^\"]+)\""#)
        var keys = Set<String>()
        for url in sourceURLs {
            let source = try String(contentsOf: url, encoding: .utf8)
            let range = NSRange(source.startIndex..., in: source)
            for match in regex.matches(in: source, range: range) {
                guard let keyRange = Range(match.range(at: 1), in: source) else { continue }
                keys.insert(String(source[keyRange]))
            }
        }
        return keys
    }
}
