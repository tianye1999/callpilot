import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallTimingTests: XCTestCase {
    func testHTTPReplaysAlreadyPublishedMarksWithEngineSnapshotWithinBodyBudget() throws {
        let timing = CallTiming(now: { 0 })
        timing.startClaim()
        for mark in CallTimingMark.allCases { timing.mark(mark, at: 86_399.99999999999) }
        timing.recordCloudDurations("cas;dur=86399999.99999999, edge_reserve;dur=86399999.99999999, turn;dur=86399999.99999999, sdp_relay;dur=86399999.99999999, claim_finalize;dur=86399999.99999999, total;dur=86399999.99999999")
        timing.recordAudioEngine(CallAudioEngineSnapshot(running: false, recording: true, playing: true,
            inputEnabled: false, inputRunning: false, outputEnabled: false, outputRunning: false,
            inputMuted: false, muteMode: -1))
        while timing.nextBatch() != nil {}
        let lastSequence = timing.sequence
        let batches = try timing.httpBatches().map(decode)
        XCTAssertLessThanOrEqual(batches.count, 8)
        let body = try JSONSerialization.data(withJSONObject: ["claimId": "claim_abcdefghijkl", "clientTiming": batches])
        XCTAssertLessThanOrEqual(body.count, 4096)
        var received = Set<String>()
        for batch in batches {
            XCTAssertGreaterThan(try XCTUnwrap(batch["seq"] as? Int), lastSequence)
            received.formUnion(try XCTUnwrap(batch["marks"] as? [String: Double]).keys)
            let engine = try XCTUnwrap(batch["audioEngine"] as? [String: Any])
            XCTAssertEqual(engine["recording"] as? Bool, true)
            XCTAssertEqual(engine["running"] as? Bool, false)
        }
        XCTAssertTrue(Set(CallTimingMark.allCases.map(\.rawValue)).isSubset(of: received))
    }

    func testCloudDurationsRemainDurationsAcrossClientOriginChanges() throws {
        var now: TimeInterval = 100
        let timing = CallTiming(now: { now })
        timing.recordCloudDurations("cas;dur=12.5, edge_reserve;dur=90, turn;dur=250, sdp_relay;dur=800, claim_finalize;dur=3, total;dur=999")
        now = 200
        timing.startClaim()
        let batch = try decode(XCTUnwrap(timing.nextBatch()))
        let marks = try XCTUnwrap(batch["marks"] as? [String: Double])
        XCTAssertEqual(marks["cloud_cas_ms"], 12.5)
        XCTAssertEqual(marks["cloud_edge_reserve_ms"], 90)
        XCTAssertEqual(marks["cloud_turn_ms"], 250)
        XCTAssertEqual(marks["cloud_sdp_relay_ms"], 800)
        XCTAssertEqual(marks["cloud_claim_finalize_ms"], 3)
        XCTAssertEqual(marks["cloud_total_ms"], 999)
        XCTAssertEqual(marks["claim_start"], 0)
        XCTAssertNil(timing.nextBatch())
    }

    func testCloudTimingRejectsMalformedUnknownNonfiniteAndOutOfBoundsValues() throws {
        let timing = CallTiming(now: { 0 })
        timing.recordCloudDurations("cas;dur=nan, turn;dur=inf, total;dur=-1, sdp_relay;dur=86400001, unknown;dur=20, claim_finalize;dur=oops")
        XCTAssertNil(timing.nextBatch())
        timing.recordCloudDurations("cas;dur=7;dur=8, total;dur=5, total;dur=9, turn;desc=\"ignored\";dur=20")
        let batch = try decode(XCTUnwrap(timing.nextBatch()))
        let marks = try XCTUnwrap(batch["marks"] as? [String: Double])
        XCTAssertNil(marks["cloud_cas_ms"])
        XCTAssertEqual(marks["cloud_total_ms"], 5)
        XCTAssertEqual(marks["cloud_turn_ms"], 20)
        timing.recordCloudDurations("total;dur=99")
        XCTAssertNil(timing.nextBatch())
    }

    func testClaimOriginPreservesEarlierAnswerAsNegativeOffset() throws {
        var now: TimeInterval = 10
        let timing = CallTiming(now: { now })
        timing.mark(.answerAction)
        now = 10.25
        timing.startClaim()
        now = 10.75
        timing.mark(.claimResponse)
        let batch = try decode(XCTUnwrap(timing.nextBatch()))
        let marks = try XCTUnwrap(batch["marks"] as? [String: Double])
        XCTAssertEqual(marks["answer_action"], -250)
        XCTAssertEqual(marks["claim_start"], 0)
        XCTAssertEqual(marks["claim_response"], 500)
    }

    func testBatchesHaveStableAttemptIncrementalSequenceAndNoDuplicateMarks() throws {
        var now: TimeInterval = 20
        let timing = CallTiming(now: { now })
        XCTAssertNil(timing.nextBatch())
        timing.mark(.dialStart)
        let first = try decode(XCTUnwrap(timing.nextBatch()))
        XCTAssertEqual(first["type"] as? String, "client_timing")
        XCTAssertEqual(first["v"] as? Int, 1)
        XCTAssertEqual(first["seq"] as? Int, 1)
        let attempt = try XCTUnwrap(first["attempt"] as? String)
        XCTAssertNotNil(UUID(uuidString: attempt))
        XCTAssertEqual(Set(first.keys), ["type", "v", "attempt", "seq", "marks"])
        XCTAssertNil(timing.nextBatch())

        now = 20.125
        timing.mark(.dialStart)
        timing.mark(.connectStart)
        let second = try decode(XCTUnwrap(timing.nextBatch()))
        XCTAssertEqual(second["attempt"] as? String, attempt)
        XCTAssertEqual(second["seq"] as? Int, 2)
        XCTAssertEqual(second["marks"] as? [String: Double], ["connect_start": 125])
        XCTAssertNil(timing.nextBatch())
        XCTAssertEqual(timing.sequence, 2)
    }

    func testAllEnumMarksFitBoundedBatchesAndCannotGrowThroughRepeats() throws {
        let timing = CallTiming(now: { 0 })
        for mark in CallTimingMark.allCases {
            timing.mark(mark)
        }
        var received = Set<String>()
        var batches = 0
        while let data = timing.nextBatch() {
            batches += 1
            XCTAssertLessThanOrEqual(data.count, CallTiming.maxBatchBytes)
            let batch = try decode(data)
            let marks = try XCTUnwrap(batch["marks"] as? [String: Double])
            XCTAssertLessThanOrEqual(marks.count, CallTiming.maxBatchMarks)
            XCTAssertTrue(received.isDisjoint(with: marks.keys))
            received.formUnion(marks.keys)
            XCTAssertEqual(batch["seq"] as? Int, batches)
            XCTAssertLessThanOrEqual(batches, CallTiming.maxBatches)
        }
        XCTAssertEqual(received, Set(CallTimingMark.allCases.map(\.rawValue)))
        XCTAssertLessThanOrEqual(received.count, 64)
        for _ in 0..<100 {
            for mark in CallTimingMark.allCases { timing.mark(mark, at: 100) }
        }
        XCTAssertNil(timing.nextBatch())
        XCTAssertEqual(timing.sequence, batches)
    }

    func testWireOffsetsAreClampedWithoutLeakingExtraFields() throws {
        let timing = CallTiming(now: { 0 })
        timing.mark(.answerAction, at: -100)
        timing.mark(.disconnect, at: 100_000)
        let batch = try decode(XCTUnwrap(timing.nextBatch()))
        XCTAssertEqual(batch["marks"] as? [String: Double], [
            "answer_action": -60_000,
            "disconnect": 86_400_000,
        ])
        XCTAssertEqual(Set(batch.keys), ["type", "v", "attempt", "seq", "marks"])
    }

    func testEveryAttemptGetsANewRandomIdentifier() {
        let first = CallTiming()
        let second = CallTiming()
        XCTAssertNotEqual(first.attempt, second.attempt)
    }

    private func decode(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
}
