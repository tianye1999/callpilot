import Foundation
import XCTest
@testable import CallPilot

final class VoipPushPayloadTests: XCTestCase {
    func testDecodesOpaqueVersionedOfferPayload() throws {
        let payload: [AnyHashable: Any] = [
            "v": 1,
            "type": "inbound.offer",
            "offerId": "offer_abcdefghijkl",
            "callUUID": "12345678-1234-4abc-8def-1234567890ab",
            "expiresAtUnixMs": 1_800_000_000_000,
        ]

        XCTAssertEqual(
            VoipPushPayload.decode(payload),
            VoipPushPayload(
                offerId: "offer_abcdefghijkl",
                callUUID: try XCTUnwrap(UUID(
                    uuidString: "12345678-1234-4abc-8def-1234567890ab"
                )),
                expiresAtUnixMs: 1_800_000_000_000
            )
        )
    }

    func testRejectsWrongVersionTypeIdentifiersAndBooleanTimestamp() {
        let valid: [AnyHashable: Any] = [
            "v": 1,
            "type": "inbound.offer",
            "offerId": "offer_abcdefghijkl",
            "callUUID": "12345678-1234-4abc-8def-1234567890ab",
            "expiresAtUnixMs": 1_800_000_000_000,
        ]
        for invalid in [
            replacing(valid, key: "v", value: 2),
            replacing(valid, key: "type", value: "inbound.revoke"),
            replacing(valid, key: "offerId", value: "not-opaque"),
            replacing(valid, key: "callUUID", value: "not-a-uuid"),
            replacing(valid, key: "expiresAtUnixMs", value: true),
        ] {
            XCTAssertNil(VoipPushPayload.decode(invalid))
        }
    }

    private func replacing(
        _ source: [AnyHashable: Any],
        key: String,
        value: Any
    ) -> [AnyHashable: Any] {
        var copy = source
        copy[key] = value
        return copy
    }
}
