import Foundation
import XCTest
@testable import CallPilot

final class CallKitCallRegistryTests: XCTestCase {
    private let now: Int64 = 1_800_000_000_000

    func testRegisterRejectsExpiredDuplicateOfferAndDuplicateUUID() {
        var registry = CallKitCallRegistry()
        let first = payload(
            offerId: "offer_aaaaaaaaaaaa",
            callUUID: "12345678-1234-4abc-8def-1234567890ab"
        )

        XCTAssertTrue(registry.register(first, nowUnixMs: now))
        XCTAssertFalse(registry.register(first, nowUnixMs: now))
        XCTAssertFalse(registry.register(payload(
            offerId: "offer_aaaaaaaaaaaa",
            callUUID: "22345678-1234-4abc-8def-1234567890ab"
        ), nowUnixMs: now))
        XCTAssertFalse(registry.register(payload(
            offerId: "offer_bbbbbbbbbbbb",
            callUUID: "12345678-1234-4abc-8def-1234567890ab"
        ), nowUnixMs: now))
        XCTAssertFalse(registry.register(VoipPushPayload(
            offerId: "offer_cccccccccccc",
            callUUID: UUID(),
            expiresAtUnixMs: now
        ), nowUnixMs: now))
    }

    func testAnswerAndConnectionTransitionsAreSingleUse() throws {
        var registry = CallKitCallRegistry()
        let incoming = payload(
            offerId: "offer_aaaaaaaaaaaa",
            callUUID: "12345678-1234-4abc-8def-1234567890ab"
        )
        XCTAssertTrue(registry.register(incoming, nowUnixMs: now))

        XCTAssertEqual(
            registry.beginAnswer(callUUID: incoming.callUUID)?.offerId,
            incoming.offerId
        )
        XCTAssertNil(registry.beginAnswer(callUUID: incoming.callUUID))
        XCTAssertTrue(registry.markConnected(callUUID: incoming.callUUID))
        XCTAssertFalse(registry.markConnected(callUUID: incoming.callUUID))
        XCTAssertEqual(registry.phase(callUUID: incoming.callUUID), .active)
    }

    func testReconcileEndsOnlyMissingRingingCalls() throws {
        var registry = CallKitCallRegistry()
        let ringing = payload(
            offerId: "offer_aaaaaaaaaaaa",
            callUUID: "12345678-1234-4abc-8def-1234567890ab"
        )
        let answering = payload(
            offerId: "offer_bbbbbbbbbbbb",
            callUUID: "22345678-1234-4abc-8def-1234567890ab"
        )
        XCTAssertTrue(registry.register(ringing, nowUnixMs: now))
        XCTAssertTrue(registry.register(answering, nowUnixMs: now))
        XCTAssertNotNil(registry.beginAnswer(callUUID: answering.callUUID))

        let ended = registry.reconcile(
            openOfferIds: [],
            nowUnixMs: now + 1
        )

        XCTAssertEqual(ended, [ringing.callUUID])
        XCTAssertNil(registry.phase(callUUID: ringing.callUUID))
        XCTAssertEqual(registry.phase(callUUID: answering.callUUID), .answering)
    }

    private func payload(offerId: String, callUUID: String) -> VoipPushPayload {
        VoipPushPayload(
            offerId: offerId,
            callUUID: UUID(uuidString: callUUID)!,
            expiresAtUnixMs: now + 60_000
        )
    }
}
