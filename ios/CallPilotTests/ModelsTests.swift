import CoreFoundation
import Foundation
import XCTest
@testable import CallPilot

final class ModelsTests: XCTestCase {
    func testJSONTimestampFixturesDistinguishNumbersFromBooleans() throws {
        let payload = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: Data(#"{"zero":0,"one":1,"false":false,"true":true}"#.utf8)
            ) as? [String: Any]
        )

        for key in ["zero", "one"] {
            let number = try XCTUnwrap(payload[key] as? NSNumber)
            XCTAssertNotEqual(CFGetTypeID(number), CFBooleanGetTypeID())
        }
        for key in ["false", "true"] {
            let boolean = try XCTUnwrap(payload[key] as? NSNumber)
            XCTAssertEqual(CFGetTypeID(boolean), CFBooleanGetTypeID())
        }
    }

    func testDeviceStatusRequiresEdgeAndModemOnline() {
        // Android parity: DialScreenTest.`拨号按钮要求号码有效且线路就绪`.
        XCTAssertTrue(HostedDeviceStatus(connected: true, modemOnline: true).lineReady)
        XCTAssertFalse(HostedDeviceStatus(connected: false, modemOnline: true).lineReady)
        XCTAssertFalse(HostedDeviceStatus(connected: true, modemOnline: false).lineReady)
    }

    func testHostedSessionDescriptionRedactsToken() {
        // Android parity: CredentialRedactionTest.`HostedCallSession 不泄露 token`.
        let session = HostedCallSession(
            sessionId: "claim_abcdefghijkl",
            livekitURL: "wss://lk.example.com",
            token: "jwt-plaintext",
            expiresAt: 9_999
        )

        XCTAssertTrue(session.description.contains("claim_abcdefghijkl"))
        XCTAssertFalse(session.description.contains("jwt-plaintext"))
        XCTAssertFalse(String(reflecting: session).contains("jwt-plaintext"))
    }
}
