import Foundation
import XCTest
@testable import CallPilot

final class ClaimNetworkMetricsTests: XCTestCase {
    func testTransactionDatesProjectOntoTheSameMonotonicClock() throws {
        let wall = Date(timeIntervalSince1970: 1_000)
        let metrics = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 50)
        metrics.record(.init(
            dnsStart: wall.addingTimeInterval(0.1), dnsEnd: wall.addingTimeInterval(0.2),
            connectStart: wall.addingTimeInterval(0.3), connectEnd: wall.addingTimeInterval(0.6),
            tlsStart: wall.addingTimeInterval(0.4), tlsEnd: wall.addingTimeInterval(0.5),
            requestStart: wall.addingTimeInterval(0.7), firstByte: wall.addingTimeInterval(0.9)
        ))
        let expected: [CallTimingMark: TimeInterval] = [
            .claimDNSStart: 50.1, .claimDNSEnd: 50.2,
            .claimConnectStart: 50.3, .claimConnectEnd: 50.6,
            .claimTLSStart: 50.4, .claimTLSEnd: 50.5,
            .claimRequestStart: 50.7, .claimFirstByte: 50.9,
        ]
        XCTAssertEqual(Set(metrics.marks.keys), Set(expected.keys))
        for (mark, value) in expected {
            XCTAssertEqual(try XCTUnwrap(metrics.marks[mark]), value, accuracy: 0.000_001)
        }
    }

    func testAbsentTransactionDatesDoNotInventNetworkStages() {
        let metrics = ClaimNetworkMetrics(wallTime: Date(timeIntervalSince1970: 0), monotonicTime: 0)
        metrics.record(.init())
        XCTAssertTrue(metrics.marks.isEmpty)
    }

    func testReusedConnectionUsesRequestStartAndDoesNotInventDNSOrTLS() throws {
        let wall = Date(timeIntervalSince1970: 1_000)
        let metrics = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 20)
        metrics.record(.init(requestStart: wall.addingTimeInterval(0.3),
                             firstByte: wall.addingTimeInterval(0.4),
                             fetchStart: wall.addingTimeInterval(0.1), reused: true))
        XCTAssertEqual(try XCTUnwrap(metrics.marks[.claimConnectionReused]), 20.3, accuracy: 0.000_001)
        XCTAssertNil(metrics.marks[.claimDNSStart])
        XCTAssertNil(metrics.marks[.claimTLSStart])
        XCTAssertEqual(Set(metrics.marks.keys), [.claimConnectionReused, .claimRequestStart, .claimFirstByte])
    }

    func testReusedConnectionFallsBackToFetchStartOnlyWhenRequestStartIsMissing() throws {
        let wall = Date(timeIntervalSince1970: 1_000)
        let metrics = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 20)
        metrics.record(.init(fetchStart: wall.addingTimeInterval(0.2), reused: true))
        XCTAssertEqual(try XCTUnwrap(metrics.marks[.claimConnectionReused]), 20.2, accuracy: 0.000_001)
        XCTAssertNil(metrics.marks[.claimRequestStart])

        let missing = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 20)
        missing.record(.init(reused: true))
        XCTAssertNil(missing.marks[.claimConnectionReused])
    }

    func testNonReusedConnectionDoesNotEmitAReusedMark() {
        let wall = Date(timeIntervalSince1970: 1_000)
        let metrics = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 20)
        metrics.record(.init(requestStart: wall, fetchStart: wall, reused: false))
        XCTAssertNil(metrics.marks[.claimConnectionReused])
        XCTAssertEqual(metrics.marks[.claimRequestStart], 20)
    }

    func testRecordAndSnapshotsAreSafeAcrossConcurrentDelegateCallbacks() {
        let wall = Date(timeIntervalSince1970: 1_000)
        let metrics = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 20)
        DispatchQueue.concurrentPerform(iterations: 64) { _ in
            metrics.record(.init(dnsStart: wall, dnsEnd: wall.addingTimeInterval(0.1)))
            _ = metrics.marks
        }
        var snapshot = metrics.marks
        metrics.record(.init(firstByte: wall.addingTimeInterval(0.2)))
        XCTAssertEqual(snapshot.count, 2)
        XCTAssertNil(snapshot[.claimFirstByte])
        XCTAssertNil(metrics.marks[.claimFirstByte])
        snapshot.removeValue(forKey: .claimDNSStart)
        XCTAssertEqual(snapshot.count, 1)
        XCTAssertEqual(metrics.marks.count, 2)
    }

    @MainActor
    func testProjectedNetworkDatesKeepNegativeOffsetsRelativeToClaimStart() throws {
        let wall = Date(timeIntervalSince1970: 1_000)
        let metrics = ClaimNetworkMetrics(wallTime: wall, monotonicTime: 50)
        metrics.record(.init(dnsStart: wall.addingTimeInterval(-0.25),
                             requestStart: wall.addingTimeInterval(0.125)))
        let timing = CallTiming(now: { 50 })
        timing.startClaim()
        for (mark, instant) in metrics.marks { timing.mark(mark, at: instant) }
        let batch = try XCTUnwrap(timing.nextBatch())
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: batch) as? [String: Any])
        let marks = try XCTUnwrap(payload["marks"] as? [String: Double])
        XCTAssertEqual(marks[CallTimingMark.claimDNSStart.rawValue], -250)
        XCTAssertEqual(marks[CallTimingMark.claimRequestStart.rawValue], 125)
        XCTAssertEqual(marks[CallTimingMark.claimStart.rawValue], 0)
    }
}
