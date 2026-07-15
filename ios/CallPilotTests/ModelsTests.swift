import XCTest
@testable import CallPilot

final class ModelsTests: XCTestCase {
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
