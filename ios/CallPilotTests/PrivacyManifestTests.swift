import Foundation
import XCTest

final class PrivacyManifestTests: XCTestCase {
    func testAppPrivacyManifestDeclaresRetainedMetadataWithoutTracking() throws {
        let manifestURL = repositoryRoot.appendingPathComponent(
            "ios/CallPilot/PrivacyInfo.xcprivacy"
        )
        let manifest = try XCTUnwrap(
            PropertyListSerialization.propertyList(
                from: Data(contentsOf: manifestURL),
                options: [],
                format: nil
            ) as? [String: Any]
        )

        XCTAssertEqual(manifest["NSPrivacyTracking"] as? Bool, false)
        XCTAssertNil(manifest["NSPrivacyTrackingDomains"])
        XCTAssertNil(manifest["NSPrivacyAccessedAPITypes"])

        let entries = try XCTUnwrap(
            manifest["NSPrivacyCollectedDataTypes"] as? [[String: Any]]
        )
        let entriesByType = Dictionary(
            uniqueKeysWithValues: try entries.map { entry in
                let type = try XCTUnwrap(entry["NSPrivacyCollectedDataType"] as? String)
                return (type, entry)
            }
        )
        XCTAssertEqual(
            Set(entriesByType.keys),
            [
                "NSPrivacyCollectedDataTypeUserID",
                "NSPrivacyCollectedDataTypeDeviceID",
                "NSPrivacyCollectedDataTypeProductInteraction",
                "NSPrivacyCollectedDataTypeOtherDiagnosticData",
            ]
        )
        for entry in entriesByType.values {
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypeLinked"] as? Bool, true)
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypeTracking"] as? Bool, false)
            XCTAssertEqual(
                entry["NSPrivacyCollectedDataTypePurposes"] as? [String],
                ["NSPrivacyCollectedDataTypePurposeAppFunctionality"]
            )
        }
    }

    private var repositoryRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }
}
