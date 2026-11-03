import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallTimingTests: XCTestCase {
    func testCaptureAndDecodeStatsAreBoundedValidatedAndContentFree() throws {
        let packet = try XCTUnwrap(CallRtpStats.packet(reports: [
            CallRtpReport(id: "source", type: "media-source", values: ["kind": "audio", "audioLevel": 0.25, "totalAudioEnergy": 2.5, "totalSamplesDuration": 10.0, "deviceName": "private"]),
            CallRtpReport(id: "out", type: "outbound-rtp", values: ["kind": "audio", "headerBytesSent": 120, "retransmittedPacketsSent": 0, "active": true]),
            CallRtpReport(id: "in", type: "inbound-rtp", values: ["kind": "audio", "audioLevel": 0.5, "concealedSamples": 12, "totalSamplesReceived": 48000, "jitterBufferEmittedCount": 100]),
        ], sequence: 10, elapsedMs: 60000))
        let object = try XCTUnwrap(CallRtpStats.validatedObject(packet))
        XCTAssertEqual((object["source"] as? [String: Any])?["totalAudioEnergy"] as? Double, 2.5)
        XCTAssertEqual((object["sender"] as? [String: Any])?["active"] as? Bool, true)
        XCTAssertEqual((object["receiver"] as? [String: Any])?["totalSamplesReceived"] as? Int, 48000)
        XCTAssertLessThanOrEqual(packet.count, 1024)
        XCTAssertFalse(String(decoding: packet, as: UTF8.self).contains("private"))
        var bad = object
        bad["source"] = ["audioLevel": 2, "totalAudioEnergy": 0, "totalSamplesDuration": 0]
        XCTAssertNil(CallRtpStats.validatedObject(try JSONSerialization.data(withJSONObject: bad)))
        let snapshot = CallAudioSessionSnapshot(category: "AVAudioSessionCategoryPlayAndRecord", mode: "AVAudioSessionModeVoiceChat", inputAvailable: true, inputNumberOfChannels: 1, inputs: ["MicrophoneBuiltIn"], outputs: ["Receiver"])
        XCTAssertTrue(snapshot.isValid)
    }

    func testRtpSamplerUsesEventsStopsAtTenAndTakesFastTerminalSample() async {
        var instant: TimeInterval = 0
        var requests: [(Int, Int)] = []
        var continuation: CheckedContinuation<Void, Never>?
        let ticks = (0..<9).map { expectation(description: "sampler awaits tick \($0)") }
        var tickIndex = 0
        let sampler = CallRtpSampler(now: { instant }, sleep: { duration in
            XCTAssertEqual(duration, .seconds(2))
            await withCheckedContinuation { value in
                continuation = value
                ticks[tickIndex].fulfill()
                tickIndex += 1
            }
        }, request: { requests.append(($0, $1)) })
        sampler.start()
        XCTAssertEqual(requests.count, 1)
        for index in 0..<8 {
            await fulfillment(of: [ticks[index]], timeout: 1)
            instant += 2
            continuation?.resume()
        }
        await fulfillment(of: [ticks[8]], timeout: 1)
        sampler.finish()
        XCTAssertEqual(requests.map(\.0), Array(1...10))
        XCTAssertEqual(requests.last?.1, 16_000)
        continuation?.resume()
        sampler.finish()
        sampler.start()
        XCTAssertEqual(requests.count, 10)

        var fast: [Int] = []
        let quick = CallRtpSampler(sleep: { _ in XCTFail("commit before first periodic tick") },
                                  request: { seq, _ in fast.append(seq) })
        quick.start()
        quick.finish()
        XCTAssertEqual(fast, [1, 2])
        var early: [Int] = []
        let beforeContinuation = CallRtpSampler(sleep: { _ in XCTFail("terminal sampler cannot restart") },
                                               request: { seq, _ in early.append(seq) })
        beforeContinuation.finish()
        beforeContinuation.start()
        XCTAssertEqual(early, [1])
    }

    func testRtpReportKeepsOnlyAudioCountersAndSelectedPairWithoutIdentifiers() throws {
        let packet = try XCTUnwrap(CallRtpStats.packet(reports: [
            CallRtpReport(id: "out", type: "outbound-rtp", values: ["kind": "audio", "packetsSent": 8, "bytesSent": 160]),
            CallRtpReport(id: "video", type: "outbound-rtp", values: ["kind": "video", "packetsSent": 999]),
            CallRtpReport(id: "in", type: "inbound-rtp", values: ["kind": "audio", "packetsReceived": 7, "bytesReceived": 140, "packetsLost": -1]),
            CallRtpReport(id: "remote", type: "remote-inbound-rtp", values: ["kind": "audio", "roundTripTime": 0.025]),
            CallRtpReport(id: "transport", type: "transport", values: ["selectedCandidatePairId": "pair-private"]),
            CallRtpReport(id: "pair-private", type: "candidate-pair", values: ["localCandidateId": "local-private", "remoteCandidateId": "remote-private", "state": "succeeded"]),
            CallRtpReport(id: "local-private", type: "local-candidate", values: ["candidateType": "relay", "address": "198.51.100.7", "port": 9999]),
            CallRtpReport(id: "remote-private", type: "remote-candidate", values: ["candidateType": "host"]),
        ], sequence: 1, elapsedMs: 2000))
        XCTAssertLessThanOrEqual(packet.count, CallRtpStats.maxBytes)
        let object = try decode(packet)
        XCTAssertEqual(object["outPackets"] as? Int, 8)
        XCTAssertEqual(object["inPackets"] as? Int, 7)
        XCTAssertEqual(object["lost"] as? Int, -1)
        XCTAssertEqual(object["rttMs"] as? Double, 25)
        XCTAssertEqual(object["localCandidate"] as? String, "relay")
        XCTAssertEqual(object["pairState"] as? String, "succeeded")
        let text = String(decoding: packet, as: UTF8.self)
        XCTAssertFalse(text.contains("private"))
        XCTAssertFalse(text.contains("198.51.100"))
        XCTAssertFalse(text.contains("9999"))
    }

    func testRtpMissingOrUnsafeValuesStayNullWithinPacketLimit() throws {
        let object = try decode(XCTUnwrap(CallRtpStats.packet(reports: [
            CallRtpReport(id: "out", type: "outbound-rtp", values: ["kind": "audio", "packetsSent": -1,
                "bytesSent": 9_007_199_254_740_992.0]),
        ], sequence: 10, elapsedMs: 60_000)))
        for key in ["outPackets", "outBytes", "inPackets", "inBytes", "lost", "rttMs", "localCandidate", "remoteCandidate", "pairState"] {
            XCTAssertTrue(object[key] is NSNull, key)
        }
        XCTAssertNil(CallRtpStats.packet(reports: [], sequence: 11, elapsedMs: 1))
    }

    func testPrepareAudioGateDoesNotActivateOrResetAnAlreadyActivatedOwner() throws {
        let gate = CallDirectAudioGate(), owner = UUID()
        XCTAssertTrue(try gate.prepare(owner))
        XCTAssertFalse(gate.active)
        try gate.activate(owner)
        XCTAssertTrue(gate.active)
        XCTAssertFalse(try gate.prepare(owner))
        XCTAssertTrue(gate.active)
        XCTAssertThrowsError(try gate.prepare(UUID()))
        XCTAssertTrue(gate.release(owner))
        XCTAssertFalse(gate.active)
        XCTAssertTrue(try gate.prepare(UUID()), "pending old peer close is not a registration lock")
    }

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
        let batches = try timing.httpBatches(reservingBytes: CallRtpStats.maxBytes).map(decode)
        XCTAssertLessThanOrEqual(batches.count, 8)
        let rtp = try decode(XCTUnwrap(CallRtpStats.packet(reports: [
            CallRtpReport(id: "out", type: "outbound-rtp", values: ["kind": "audio", "packetsSent": 9_007_199_254_740_991, "bytesSent": 9_007_199_254_740_991]),
            CallRtpReport(id: "in", type: "inbound-rtp", values: ["kind": "audio", "packetsReceived": 9_007_199_254_740_991, "bytesReceived": 9_007_199_254_740_991, "packetsLost": -9_007_199_254_740_991]),
            CallRtpReport(id: "remote", type: "remote-inbound-rtp", values: ["kind": "audio", "roundTripTime": 1e300]),
        ], sequence: 10, elapsedMs: 60_000)))
        let body = try JSONSerialization.data(withJSONObject: ["claimId": "claim_abcdefghijkl", "clientTiming": batches, "clientRtpStats": rtp])
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
