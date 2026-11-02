import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class ClaimPrewarmerTests: XCTestCase {
    func testPushReportsCallKitBeforeSchedulingNonblockingPrewarm() throws {
        let coordinator = try source("Call/CallKitCoordinator.swift")
        let push = try section(coordinator, from: "didReceiveIncomingPushWith payload",
                               to: "extension CallKitCoordinator: CXProviderDelegate")
        XCTAssertTrue(push.contains("MainActor.assumeIsolated"))
        XCTAssertTrue(push.contains("self.report(decoded, completion: completionBox)"))
        XCTAssertFalse(push.contains("await "))
        XCTAssertFalse(push.contains("prewarm"))
        let report = try section(coordinator, from: "private func report(", to: "private func scheduleExpiration")
        let systemReport = try XCTUnwrap(report.range(of: "provider.reportNewIncomingCall"))
        let delegate = try XCTUnwrap(report.range(of: "self.delegate?.callKitDidReceiveOffer(offer)"))
        let completion = try XCTUnwrap(report.range(of: "completion.call()", options: .backwards))
        XCTAssertLessThan(systemReport.lowerBound, delegate.lowerBound)
        XCTAssertLessThan(delegate.lowerBound, completion.lowerBound)
        XCTAssertFalse(report.contains("await "))

        let app = try source("AppModel.swift")
        let reception = try section(app, from: "func callKitDidReceiveOffer", to: "func callKitDidRequestAnswer")
        XCTAssertTrue(reception.contains("claimPrewarmer.receive(offer) { await client.prewarmClaimConnection() }"))
        XCTAssertFalse(reception.contains("await claimPrewarmer.receive"))
        let prewarmer = try source("Call/ClaimPrewarmer.swift")
        let receive = try section(prewarmer, from: "func receive(", to: "func timing(")
        let task = try XCTUnwrap(receive.range(of: "let task = Task {"))
        let operation = try XCTUnwrap(receive.range(of: "await prewarm()"))
        XCTAssertLessThan(task.lowerBound, operation.lowerBound)
    }

    func testAnswerAndClaimShareThePrewarmTimingObject() throws {
        let app = try source("AppModel.swift")
        let answer = try section(app, from: "func callKitDidRequestAnswer", to: "func callKitDidRequestDecline")
        XCTAssertTrue(answer.contains("claimPrewarmer.timing(for: offer.offerId) ?? CallTiming()"))
        XCTAssertTrue(answer.contains("performTakeover(offer, usesCallKit: true, timing: timing)"))
        let media = try source("Call/CallMediaSession.swift")
        let takeover = try section(media, from: "func startTakeover(", to: "func prepareCallKitAnswer")
        let baseline = try XCTUnwrap(takeover.range(of: "timing.startClaim()"))
        let claim = try XCTUnwrap(takeover.range(of: "client.claimInboundOffer(offerId: offerId, timing: timing)"))
        XCTAssertLessThan(baseline.lowerBound, claim.lowerBound)
    }

    func testPrewarmingDefaultsOffAndDoesNotRunOrCacheAnOffer() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        var calls = 0
        XCTAssertFalse(prewarmer.enabled)
        XCTAssertNil(prewarmer.receive(offer()) { calls += 1 })
        XCTAssertEqual(calls, 0)
        XCTAssertNil(prewarmer.timing(for: "offer_a"))
    }

    func testAnEnabledOfferWarmsOnceBeforeAndAfterCompletion() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        var calls = 0
        let first = try XCTUnwrap(prewarmer.receive(offer()) { calls += 1 })
        XCTAssertNil(prewarmer.receive(offer()) { calls += 1 })
        let timing = try XCTUnwrap(prewarmer.timing(for: "offer_a"))
        await first.value
        XCTAssertEqual(calls, 1)
        XCTAssertNil(prewarmer.receive(offer()) { calls += 1 })
        XCTAssertTrue(timing === prewarmer.timing(for: "offer_a"))
    }

    func testExpiredOffersAreRejectedIncludingTheExactDeadline() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        var calls = 0
        XCTAssertNil(prewarmer.receive(offer(expiresAt: 999)) { calls += 1 })
        XCTAssertNil(prewarmer.receive(offer(expiresAt: 1_000)) { calls += 1 })
        XCTAssertEqual(calls, 0)
        XCTAssertNil(prewarmer.timing(for: "offer_a"))
    }

    func testEnabledSettingSurvivesColdReconstructionForTheSameBinding() throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = ClaimPrewarmer(defaults: defaults)
        first.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        first.setEnabled(true)
        let restored = ClaimPrewarmer(defaults: defaults)
        restored.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        XCTAssertTrue(restored.enabled)
        restored.setEnabled(false)
        let disabled = ClaimPrewarmer(defaults: defaults)
        disabled.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        XCTAssertFalse(disabled.enabled)
    }

    func testASeenOfferIsNotPrewarmedAgainAfterColdReconstruction() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let first = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        first.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        first.setEnabled(true)
        var calls = 0
        let task = try XCTUnwrap(first.receive(offer()) { calls += 1 })
        await task.value
        let restored = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        restored.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        XCTAssertTrue(restored.enabled)
        XCTAssertNil(restored.receive(offer()) { calls += 1 })
        XCTAssertEqual(calls, 1)
        XCTAssertNil(restored.timing(for: "offer_a"))
    }

    func testDisabledReceptionDoesNotConsumeTheOfferDeduplicationSlot() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        var calls = 0
        XCTAssertNil(prewarmer.receive(offer()) { calls += 1 })
        prewarmer.setEnabled(true)
        let task = try XCTUnwrap(prewarmer.receive(offer()) { calls += 1 })
        await task.value
        XCTAssertEqual(calls, 1)
    }

    func testExpiredSeenOffersAreRemovedBeforeDeduplication() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        var now: Int64 = 1_000
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { now })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        var calls = 0
        let first = try XCTUnwrap(prewarmer.receive(offer(expiresAt: 2_000)) { calls += 1 })
        await first.value
        now = 2_001
        let next = try XCTUnwrap(prewarmer.receive(offer(expiresAt: 3_000)) { calls += 1 })
        await next.value
        XCTAssertEqual(calls, 2)
    }

    func testGatewayAndDeviceChangesDoNotInheritOptInOrOfferTiming() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        let task = try XCTUnwrap(prewarmer.receive(offer()) {})
        await task.value
        prewarmer.bind(gatewayURL: "https://other.example.test", deviceId: "device_a")
        XCTAssertFalse(prewarmer.enabled)
        XCTAssertNil(prewarmer.timing(for: "offer_a"))
        prewarmer.setEnabled(true)
        prewarmer.bind(gatewayURL: "https://other.example.test", deviceId: "device_b")
        XCTAssertFalse(prewarmer.enabled)
        XCTAssertNil(prewarmer.timing(for: "offer_a"))
    }

    func testClearErasesCachedTimingAndPersistedOptIn() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        let task = try XCTUnwrap(prewarmer.receive(offer()) {})
        await task.value
        prewarmer.clear()
        XCTAssertFalse(prewarmer.enabled)
        XCTAssertNil(prewarmer.timing(for: "offer_a"))
        let restored = ClaimPrewarmer(defaults: defaults)
        restored.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        XCTAssertFalse(restored.enabled)
    }

    func testPrewarmMarksUseTheSameTimingAndPrecedeTheClaimBaseline() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { 1_000 })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        let task = try XCTUnwrap(prewarmer.receive(offer()) {})
        let timing = try XCTUnwrap(prewarmer.timing(for: "offer_a"))
        await task.value
        timing.startClaim()
        let batch = try XCTUnwrap(timing.nextBatch())
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: batch) as? [String: Any])
        let marks = try XCTUnwrap(payload["marks"] as? [String: Double])
        let start = try XCTUnwrap(marks[CallTimingMark.prewarmStart.rawValue])
        let end = try XCTUnwrap(marks[CallTimingMark.prewarmEnd.rawValue])
        XCTAssertLessThanOrEqual(start, end)
        XCTAssertLessThanOrEqual(end, 0)
        XCTAssertEqual(marks[CallTimingMark.claimStart.rawValue], 0)
        XCTAssertTrue(timing === prewarmer.timing(for: "offer_a"))
    }

    func testSeenOfferCacheIsBoundedAndExpiredEntriesMakeRoom() async throws {
        let (defaults, suite) = try makeDefaults()
        defer { defaults.removePersistentDomain(forName: suite) }
        var now: Int64 = 1_000
        let prewarmer = ClaimPrewarmer(defaults: defaults, nowMilliseconds: { now })
        prewarmer.bind(gatewayURL: "https://relay.example.test", deviceId: "device_a")
        prewarmer.setEnabled(true)
        for index in 0..<64 {
            let offer = InboundOffer(offerId: "offer_\(index)", expiresAt: 2_000)
            let task = try XCTUnwrap(prewarmer.receive(offer) {})
            await task.value
        }
        let overflow = InboundOffer(offerId: "offer_overflow", expiresAt: 3_000)
        XCTAssertNil(prewarmer.receive(overflow) {})
        now = 2_001
        let next = try XCTUnwrap(prewarmer.receive(overflow) {})
        await next.value
        XCTAssertNotNil(prewarmer.timing(for: "offer_overflow"))
        XCTAssertNil(prewarmer.timing(for: "offer_0"))
    }

    private func source(_ path: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent("CallPilot/\(path)"), encoding: .utf8)
    }

    private func section(_ source: String, from start: String, to end: String) throws -> String {
        let startRange = try XCTUnwrap(source.range(of: start))
        let endRange = try XCTUnwrap(source.range(of: end, range: startRange.upperBound..<source.endIndex))
        return String(source[startRange.lowerBound..<endRange.lowerBound])
    }

    private func offer(expiresAt: Int64 = 2_000) -> InboundOffer {
        InboundOffer(offerId: "offer_a", expiresAt: expiresAt)
    }

    private func makeDefaults() throws -> (UserDefaults, String) {
        let suite = "ClaimPrewarmerTests.\(UUID().uuidString)"
        return (try XCTUnwrap(UserDefaults(suiteName: suite)), suite)
    }
}
