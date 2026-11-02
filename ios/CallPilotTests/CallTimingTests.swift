import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallTimingTests: XCTestCase {
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
