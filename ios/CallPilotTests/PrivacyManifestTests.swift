import Foundation
import XCTest

final class PrivacyManifestTests: XCTestCase {
    func testAppPrivacyManifestDeclaresNoTrackingOrAppSideCollection() throws {
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
        XCTAssertNil(manifest["NSPrivacyCollectedDataTypes"])
        XCTAssertNil(manifest["NSPrivacyAccessedAPITypes"])
    }

    private var repositoryRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }
}
