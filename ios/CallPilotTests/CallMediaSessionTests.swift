import Foundation
import XCTest
@testable import CallPilot

@MainActor
private final class FakeDirectTransport: CallDirectMediaTransport {
    var eventHandler: ((CallMediaTransportEvent) async -> Void)?
    var prepares = 0
    var activations = 0
    var microphones = 0
    var audioStopped = false
    var failure = false
    var audioFailure = false
    var packets: [Data] = []
    var latestRtpStats: Data?
    var statsFinished = 0
    var prepareGate: ManualGate?
    var connectGate: ManualGate?
    var disconnectGate: ManualGate?
    func prepareDirect(servers: [CallIceServer]) async throws -> String {
        prepares += 1
        await prepareGate?.wait()
        return "v=0\r\noffer"
    }
    func connectDirect(answer: String) async throws {
        await connectGate?.wait()
        if failure { throw URLError(.cannotConnectToHost) }
    }
    func activateDirectAudio() throws { activations += 1 }
    func stopDirectAudio() { audioStopped = true }
    func finishRtpSampling() { statsFinished += 1 }
    func connect(url: String, token: String, enableMicrophone: Bool) async throws { XCTFail("not LiveKit") }
    func setMicrophone(enabled: Bool) async throws {
        if enabled {
            microphones += 1
            if audioFailure {
                await eventHandler?(.audioEngine(Self.snapshot(ready: false), failed: true))
                throw CallAudioError.answerExpired
            }
        }
    }
    static func snapshot(ready: Bool) -> CallAudioEngineSnapshot {
        CallAudioEngineSnapshot(running: false, recording: ready, playing: ready,
            inputEnabled: false, inputRunning: false, outputEnabled: false,
            outputRunning: false, inputMuted: false, muteMode: -1)
    }
    func publishControl(_ data: Data) async throws { packets.append(data) }
    func disconnect() async { await disconnectGate?.wait() }
    func setSpeakerphone(_ enabled: Bool) {}
}

@MainActor
final class CallMediaSessionTests: XCTestCase {
    func testOwnerDecisionIsEndedRatherThanFailureAndIdempotent() async {
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }))
        media.finishOwnerDecision("answered_elsewhere")
        media.finishOwnerDecision("answered_elsewhere")
        XCTAssertEqual(states.count, 1)
        guard case let .ended(_, reason) = states[0] else { return XCTFail("must not be a failed call") }
        XCTAssertEqual(reason, L10n.text("call.answered_elsewhere"))
        await media.stop()
    }
    func testEndedNoticeAndMatchingEdgeDepartureTerminateOnlyOnce() async throws {
        for noticeFirst in [false, true] {
            let transport = FakeCallMediaTransport()
            var terminalCount = 0
            let media = CallMediaSession(onState: { if $0.isTerminal { terminalCount += 1 } },
                transportFactory: { transport },
                audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }))
            _ = await media.startTakeover(client: try makeTakeoverClient(), offerId: "offer_abcdefghijkl", usesCallKit: true,
                onClaimed: { try media.prepareCallKitAnswer(); try media.activateCallKitAudio() }, onConnected: {})
            let handler = try XCTUnwrap(transport.eventHandler)
            await handler(.participantLeft("unrelated"))
            XCTAssertEqual(terminalCount, 0)
            let notice = CallMediaTransportEvent.data(Data(#"{"type":"ended","reason":"remote_hangup"}"#.utf8), topic: CallPilotTopic.status)
            let departure = CallMediaTransportEvent.participantLeft("edgepart_abcdefghijkl")
            await handler(noticeFirst ? notice : departure)
            XCTAssertEqual(terminalCount, 1)
            await handler(noticeFirst ? departure : notice)
            XCTAssertEqual(terminalCount, 1)
            await media.stop()
        }
    }

    func testV2AudioReadbackFailureAfterCommitTerminatesInsteadOfLeavingMutedCall() async throws {
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let client = try makeDirectClient(onFallback: { XCTFail("committed claim cannot switch transports") })
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
        _ = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {})
        let handler = direct.eventHandler
        // RTP can commit while the independent audio readback is still pending.
        await handler?(.data(Data(#"{"type":"status","status":"connected"}"#.utf8), topic: CallPilotTopic.status))
        guard case .inCall = states.last else { return XCTFail("fixture must commit before readback failure") }
        await handler?(.audioEngine(FakeDirectTransport.snapshot(ready: false), failed: true))
        guard case let .failed(_, _, code) = states.last else {
            XCTFail("post-commit audio failure must be terminal")
            await media.stop()
            return
        }
        XCTAssertEqual(code, "AUDIO_ENGINE_FAILED")
        XCTAssertTrue(direct.audioStopped)
        XCTAssertTrue(livekit.connectedTokens.isEmpty)
        let terminal = states
        await handler?(.audioEngine(FakeDirectTransport.snapshot(ready: true), failed: false))
        XCTAssertEqual(states, terminal, "late readback cannot resurrect a failed call")
        await media.stop()
    }

    func testV2CommitFinishesStatsWithoutRevokingWorkingAudio() async throws {
        let direct = FakeDirectTransport()
        let client = try makeDirectClient()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
        _ = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {})
        XCTAssertFalse(direct.audioStopped)
        media.handleConnected(label: "")
        XCTAssertEqual(direct.statsFinished, 1)
        XCTAssertFalse(direct.audioStopped)
        await media.stop()
        XCTAssertTrue(direct.audioStopped)
    }

    func testV2FallbackAndEndCarryLatestRtpEvenWhenChannelNeverOpens() async throws {
        for ending in [false, true] {
            let requestSeen = expectation(description: "HTTP carries private-safe last RTP sample")
            let stats = try XCTUnwrap(CallRtpStats.packet(reports: [], sequence: 1, elapsedMs: 1))
            let direct = FakeDirectTransport()
            direct.latestRtpStats = stats
            let client = try makeDirectClient(onFallbackBody: { body in
                if !ending {
                    XCTAssertEqual((body["clientRtpStats"] as? [String: Any])?["seq"] as? Int, 1)
                    requestSeen.fulfill()
                }
            }, onEndBody: { body in
                if ending {
                    XCTAssertEqual((body["clientRtpStats"] as? [String: Any])?["seq"] as? Int, 1)
                    requestSeen.fulfill()
                }
            })
            let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
                audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
                directTransportFactory: { direct })
            _ = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {})
            if ending { _ = media.requestHangup() }
            else { await direct.eventHandler?(.data(Data(#"{"type":"media_failed"}"#.utf8), topic: CallPilotTopic.status)) }
            await fulfillment(of: [requestSeen], timeout: 1)
            XCTAssertGreaterThan(direct.statsFinished, 0)
            await media.stop()
        }
    }

    func testOversizedOrMalformedDiagnosticsCannotBlockControlEnd() async throws {
        var requests = 0
        MediaMockURLProtocol.requestHandler = { request in
            requests += 1
            let body = try Self.directRequestFields(request)
            XCTAssertEqual(body["claimId"] as? String, "claim_abcdefghijkl")
            XCTAssertNil(body["clientRtpStats"])
            XCTAssertLessThanOrEqual(try JSONSerialization.data(withJSONObject: body).count, 4096)
            return Self.httpResponse(for: request, statusCode: 202, json: #"{"accepted":true}"#)
        }
        let client = try makeClient()
        try await client.endInboundClaim(offerId: "offer_abcdefghijkl", claimId: "claim_abcdefghijkl",
            clientTiming: [Data(repeating: 65, count: 5000), Data("not json".utf8), Data(#"{"type":"wrong"}"#.utf8)],
            clientRtpStats: Data(repeating: 65, count: 513))
        XCTAssertEqual(requests, 1)
    }

    func testV2ChannelOpenSendsTimingBeforeConnectReturns() async throws {
        let direct = FakeDirectTransport(), gate = ManualGate()
        direct.connectGate = gate
        let client = try makeDirectClient()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
        let answer = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onConnected: {}) }
        await waitUntil { gate.didStart }
        await direct.eventHandler?(.timing(.directControlReady, at: CallMonotonic.now()))
        await waitUntil { !direct.packets.isEmpty }
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: direct.packets[0]) as? [String: Any])
        let marks = try XCTUnwrap(payload["marks"] as? [String: Double])
        XCTAssertNotNil(marks["claim_start"])
        XCTAssertNotNil(marks["direct_control_ready"])
        XCTAssertNil(marks["room_connected"])
        gate.open()
        _ = await answer.value
        await media.stop()
    }

    func testV2EngineFailureAndMediaFailedImmediatelyFallbackWithTiming() async throws {
        for engineFailure in [true, false] {
            let fallback = expectation(description: "event-driven fallback")
            let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
            direct.audioFailure = engineFailure
            let client = try makeDirectClient(onFallback: { fallback.fulfill() }, onFallbackBody: { body in
                let batches = try XCTUnwrap(body["clientTiming"] as? [[String: Any]])
                XCTAssertFalse(batches.isEmpty)
                var marks: [String: Double] = [:]
                for batch in batches {
                    for (name, value) in batch["marks"] as? [String: Double] ?? [:] { marks[name] = value }
                }
                XCTAssertNotNil(marks["claim_start"])
                if engineFailure { XCTAssertNotNil(marks["audio_engine_failed"]) }
            })
            let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
                audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
                directTransportFactory: { direct })
            _ = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
                usesCallKit: true, onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
            if engineFailure { try media.activateCallKitAudio() }
            else {
                await direct.eventHandler?(.data(Data(#"{"type":"media_failed","reason":"no_phone_audio"}"#.utf8),
                                                 topic: CallPilotTopic.status))
            }
            await fulfillment(of: [fallback], timeout: 1)
            await waitUntil { livekit.connectedTokens == ["fallback-token"] }
            XCTAssertTrue(direct.audioStopped)
            await media.stop()
        }
    }

    func testAudioUnitReadinessDoesNotRequireAVAudioEngine() {
        XCTAssertTrue(FakeDirectTransport.snapshot(ready: true).platformAudioReady)
        XCTAssertFalse(FakeDirectTransport.snapshot(ready: false).platformAudioReady)
    }

    func testAudioStartupBoundUsesReadbackAndDoesNotWaitWhenRepairFails() async throws {
        var waits = 0, repairs = 0
        let failed = try await CallDirectAudioStartup.verify(snapshot: { FakeDirectTransport.snapshot(ready: false) },
            repair: { repairs += 1; return true }, wait: { duration in
                XCTAssertEqual(duration, .milliseconds(200)); waits += 1
            })
        XCTAssertFalse(failed.platformAudioReady)
        XCTAssertEqual(waits, 1)
        XCTAssertEqual(repairs, 1)
        do {
            _ = try await CallDirectAudioStartup.verify(snapshot: { FakeDirectTransport.snapshot(ready: false) },
                repair: { false }, wait: { _ in XCTFail("explicit start failure is immediate") })
            XCTFail("explicit start failure must throw")
        } catch {}
        let ready = try await CallDirectAudioStartup.verify(snapshot: { FakeDirectTransport.snapshot(ready: true) },
            repair: { XCTFail("working manual audio must not be restarted"); return false },
            wait: { _ in XCTFail("working audio never waits") })
        XCTAssertTrue(ready.platformAudioReady)
    }

    func testV2ControlEndRetriesRelayFailureButNotOwnershipRejection() async throws {
        for (status, code, expectedRequests) in [(503, "MEDIA_RELAY_UNAVAILABLE", 2),
                                                (409, "OFFER_UNAVAILABLE", 1),
                                                (401, "UNAUTHORIZED", 1)] {
            var requests = 0
            MediaMockURLProtocol.requestHandler = { request in
                requests += 1
                XCTAssertTrue(request.url?.path.hasSuffix("/media/end") == true)
                XCTAssertEqual(try Self.directRequestFields(request)["claimId"] as? String, "claim_abcdefghijkl")
                return Self.httpResponse(for: request, statusCode: status,
                    json: "{\"error\":{\"code\":\"\(code)\",\"message\":\"Unavailable\"}}")
            }
            let client = try makeClient()
            do {
                try await client.endInboundClaim(offerId: "offer_abcdefghijkl", claimId: "claim_abcdefghijkl")
                XCTFail("expected end rejection")
            } catch let error as HostedCloudError { XCTAssertEqual(error.code, code) }
            XCTAssertEqual(requests, expectedRequests)
        }
    }

    func testV2HangupAfterReservationEndsClaimBeforeSDPExists() async throws {
        let reserved = expectation(description: "claim reserved"), ended = expectation(description: "control plane end")
        let client = try makeDirectClient(onSupplement: { XCTFail("ended claim must not submit SDP") },
                                          onEnd: { ended.fulfill() })
        let direct = FakeDirectTransport(), gate = ManualGate()
        direct.prepareGate = gate
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            hangupBudget: .milliseconds(20), directTransportFactory: { direct })
        let preparing = Task { await media.startDirectPreparation(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { gate.didStart }
        let answering = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onClaimed: { reserved.fulfill() }, onConnected: {}) }
        await fulfillment(of: [reserved], timeout: 1)
        _ = media.requestHangup()
        await fulfillment(of: [ended], timeout: 1)
        gate.open()
        await preparing.value
        let accepted = await answering.value
        XCTAssertFalse(accepted)
        XCTAssertEqual(direct.microphones, 0)
        await media.stop()
    }

    func testV2HangupWhileClaimInFlightEndsLateWinnerWithoutRevivingMedia() async throws {
        let claimed = expectation(description: "claim request arrived"), ended = expectation(description: "late winner ended")
        let reply = DispatchSemaphore(value: 0)
        let client = try makeDirectClient(onClaim: { _ in claimed.fulfill(); reply.wait() },
            onSupplement: { XCTFail("late winner must not negotiate media") }, onEnd: { ended.fulfill() })
        let direct = FakeDirectTransport(), gate = ManualGate()
        direct.prepareGate = gate
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            hangupBudget: .milliseconds(20), directTransportFactory: { direct })
        let answering = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { XCTFail("hung-up answer must not fulfill") }, onConnected: {}) }
        await fulfillment(of: [claimed], timeout: 1)
        _ = media.requestHangup()
        let afterHangup = states
        reply.signal()
        await fulfillment(of: [ended], timeout: 1)
        gate.open()
        let accepted = await answering.value
        XCTAssertFalse(accepted)
        XCTAssertEqual(states, afterHangup)
        XCTAssertEqual(direct.activations, 0)
        XCTAssertEqual(direct.microphones, 0)
        await media.stop()
    }

    func testV2LosingClaimNeverEndsAnotherWinner() async throws {
        let claimed = expectation(description: "losing claim request"), ended = expectation(description: "must not end winner")
        ended.isInverted = true
        let reply = DispatchSemaphore(value: 0)
        let client = try makeDirectClient(rejectClaim: true, onClaim: { _ in claimed.fulfill(); reply.wait() },
                                          onEnd: { ended.fulfill() })
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            hangupBudget: .milliseconds(20), directTransportFactory: { FakeDirectTransport() })
        let answering = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
                                                        usesCallKit: true, onConnected: {}) }
        await fulfillment(of: [claimed], timeout: 1)
        _ = media.requestHangup()
        reply.signal()
        let accepted = await answering.value
        XCTAssertFalse(accepted)
        await fulfillment(of: [ended], timeout: 0.05)
        await media.stop()
    }

    func testV2ClaimDoesNotWaitForOldAudioDrainOrLease() async throws {
        let claimed = expectation(description: "claim before old audio is released")
        let client = try makeDirectClient(onClaim: { hasSDP in XCTAssertFalse(hasSDP); claimed.fulfill() })
        let owner = CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in })
        let oldOwner = UUID(), cleanup = ManualGate(), drain = ManualGate()
        try await owner.acquire(oldOwner, mode: .callKit)
        let releasing = owner.release(oldOwner) { await cleanup.wait() }
        await waitUntil { cleanup.didStart }
        let direct = FakeDirectTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
                                     audio: owner, directTransportFactory: { direct })
        let answering = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, prepareReservedAudio: { await drain.wait() },
            onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {}) }
        await fulfillment(of: [claimed], timeout: 1)
        XCTAssertEqual(owner.owner, oldOwner)
        XCTAssertEqual(direct.prepares, 0)
        cleanup.open()
        drain.open()
        await releasing.value
        let accepted = await answering.value
        XCTAssertTrue(accepted)
        XCTAssertEqual(direct.prepares, 1)
        await media.stop()
    }

    func testV2CancelledGatherCannotSubmitLateSDPOrReactivate() async throws {
        let reserved = expectation(description: "reserved before cancellation")
        let client = try makeDirectClient(onSupplement: { XCTFail("cancelled gather must not submit SDP") })
        let direct = FakeDirectTransport(), gate = ManualGate()
        direct.prepareGate = gate
        var states: [CallState] = []
        let owner = CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in })
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { FakeCallMediaTransport() },
                                     audio: owner, directTransportFactory: { direct })
        let preparing = Task { await media.startDirectPreparation(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { gate.didStart }
        let handler = direct.eventHandler
        let answering = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onClaimed: { reserved.fulfill() }, onConnected: {}) }
        await fulfillment(of: [reserved], timeout: 1)
        await media.stop()
        let before = states
        XCTAssertNil(owner.owner)
        gate.open()
        await preparing.value
        let accepted = await answering.value
        XCTAssertFalse(accepted)
        await handler?(.data(Data(#"{"type":"status","status":"connected"}"#.utf8), topic: CallPilotTopic.status))
        XCTAssertEqual(states, before)
        XCTAssertEqual(direct.microphones, 0)
        XCTAssertTrue(direct.audioStopped)
    }

    func testV2TURNFailureAfterReservationFallsBackWithinSameClaim() async throws {
        let fallback = expectation(description: "same claim fallback after TURN failure")
        let client = try makeDirectClient(iceUnavailable: true, onFallback: { fallback.fulfill() },
                                          onSupplement: { XCTFail("no SDP exists") })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }), directTransportFactory: { direct })
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
        XCTAssertTrue(accepted)
        await fulfillment(of: [fallback], timeout: 1)
        XCTAssertEqual(direct.prepares, 0)
        XCTAssertEqual(livekit.connectedTokens, ["fallback-token"])
        await media.stop()
    }

    func testV2FallbackRetriesOnlyExplicitRelayFailureOnce() async throws {
        for (status, code, expectedRequests) in [(504, "MEDIA_RELAY_TIMEOUT", 2),
                                                (503, "MEDIA_RELAY_UNAVAILABLE", 2),
                                                (409, "OFFER_UNAVAILABLE", 1),
                                                (401, "UNAUTHORIZED", 1)] {
            var requests = 0
            MediaMockURLProtocol.requestHandler = { request in
                requests += 1
                XCTAssertTrue(request.url?.path.hasSuffix("/media/livekit") == true)
                return Self.httpResponse(for: request, statusCode: status,
                    json: "{\"error\":{\"code\":\"\(code)\",\"message\":\"Unavailable\"}}")
            }
            let client = try makeClient()
            do {
                _ = try await client.fallbackInboundLiveKit(offerId: "offer_abcdefghijkl", claimId: "claim_abcdefghijkl")
                XCTFail("expected relay rejection")
            } catch let error as HostedCloudError { XCTAssertEqual(error.code, code) }
            XCTAssertEqual(requests, expectedRequests)
        }
    }

    func testV2DirectDisconnectBeforeEdgeCommitUsesSameClaimFallback() async throws {
        let fallback = expectation(description: "fallback before ownership commit")
        let client = try makeDirectClient(onFallback: { fallback.fulfill() })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onClaimed: { try media.prepareCallKitAnswer(); try media.activateCallKitAudio() },
            onConnected: {})
        XCTAssertTrue(accepted)
        await waitUntil { direct.microphones == 1 }
        await direct.eventHandler?(.disconnected(reason: "ICE failed after connect"))
        await fulfillment(of: [fallback], timeout: 1)
        await waitUntil { livekit.connectedTokens == ["fallback-token"] }
        XCTAssertFalse(states.contains { $0.isTerminal })
        XCTAssertTrue(direct.audioStopped)
        await media.stop()
    }

    func testV2DirectDisconnectAfterEdgeCommitDoesNotReplaceMedia() async throws {
        let client = try makeDirectClient(onFallback: { XCTFail("committed calls must not renegotiate ownership") })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onConnected: {})
        XCTAssertTrue(accepted)
        await direct.eventHandler?(.data(Data(#"{"type":"status","status":"connected"}"#.utf8),
                                           topic: CallPilotTopic.status))
        await direct.eventHandler?(.disconnected(reason: "ICE failed after commit"))
        XCTAssertTrue(states.last?.isTerminal == true)
        XCTAssertTrue(livekit.connectedTokens.isEmpty)
        await media.stop()
    }

    func testV2ICEAcceptsSixteenServersAndSecureSTUN() async throws {
        MediaMockURLProtocol.requestHandler = { request in
            let rows = Array(repeating: #"{"urls":["stuns:relay.example:5349"]}"#, count: 16).joined(separator: ",")
            return Self.httpResponse(for: request, statusCode: 200, json: "{\"iceServers\":[\(rows)]}")
        }
        let client = try makeClient()
        let servers = try await client.inboundIceServers(offerId: "offer_abcdefghijkl")
        XCTAssertEqual(servers.count, 16)
        XCTAssertEqual(servers.first?.urls, ["stuns:relay.example:5349"])
    }

    func testV2AnswerReusesInFlightGatherWithoutAnotherPeer() async throws {
        let claimed = expectation(description: "claim arrives while gather is held")
        let fulfilled = expectation(description: "CallKit claim completion does not wait for gather")
        let client = try makeDirectClient(onClaim: { hasSDP in XCTAssertFalse(hasSDP); claimed.fulfill() })
        let direct = FakeDirectTransport(), gate = ManualGate()
        direct.prepareGate = gate
        var peers = 0
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { peers += 1; return direct })
        let preparing = Task { await media.startDirectPreparation(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { gate.didStart }
        let answering = Task { await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onClaimed: { try media.prepareCallKitAnswer(); fulfilled.fulfill() }, onConnected: {}) }
        await fulfillment(of: [claimed, fulfilled], timeout: 1)
        gate.open()
        await preparing.value
        let accepted = await answering.value
        XCTAssertTrue(accepted)
        XCTAssertEqual(peers, 1)
        XCTAssertEqual(direct.prepares, 1)
        await media.stop()
    }

    func testV2AcceptedClaimWithoutAnswerCallsFallbackInsteadOfReclaiming() async throws {
        let fallback = expectation(description: "fallback after accepted relay failure")
        let client = try makeDirectClient(includeAnswer: false, onFallback: { fallback.fulfill() })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl",
            usesCallKit: true, onConnected: {})
        XCTAssertTrue(accepted)
        await fulfillment(of: [fallback], timeout: 1)
        XCTAssertTrue(direct.audioStopped)
        XCTAssertEqual(livekit.connectedTokens, ["fallback-token"])
        await media.stop()
    }

    func testV2DirectActivationBeforeAndAfterAnswer() async throws {
        for activateEarly in [false, true] {
            let client = try makeDirectClient(onClaim: { XCTAssertTrue($0) },
                                              onSupplement: { XCTFail("ready SDP belongs in the only claim HTTP") })
            let direct = FakeDirectTransport()
            let livekit = FakeCallMediaTransport()
            let device = TestCallAudioDevice()
            let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
                audio: CallAudioOwner(device: device, onCleanupError: { _ in }), directTransportFactory: { direct })
            await media.startDirectPreparation(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
            XCTAssertEqual(direct.prepares, 1)
            XCTAssertEqual(direct.activations, 0)
            XCTAssertEqual(direct.microphones, 0)
            if activateEarly { try media.activateCallKitAudio() }
            let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
                onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
            XCTAssertTrue(accepted)
            if !activateEarly { try media.activateCallKitAudio() }
            await waitUntil { direct.microphones == 1 }
            XCTAssertEqual(direct.activations, 1)
            XCTAssertEqual(direct.prepares, 1)
            XCTAssertEqual(device.activateCount, 1, "CallKit reasserts category and SDK availability after the direct gate")
            XCTAssertFalse(livekit.operations.contains(.connect))
            await media.stop()
        }
    }

    func testV2FailedICEFallsBackOnceWithoutReclaiming() async throws {
        let fallback = expectation(description: "fallback route")
        let client = try makeDirectClient(onFallback: { fallback.fulfill() })
        let direct = FakeDirectTransport()
        direct.failure = true
        let livekit = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }), directTransportFactory: { direct })
        await media.startDirectPreparation(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { try media.prepareCallKitAnswer(); try media.activateCallKitAudio() }, onConnected: {})
        XCTAssertTrue(accepted)
        await fulfillment(of: [fallback], timeout: 1)
        XCTAssertTrue(direct.audioStopped)
        XCTAssertEqual(livekit.connectedTokens, ["fallback-token"])
        await media.stop()
    }

    func testV2WatchdogDoesNotWaitForUncooperativeDisconnectOrLateConnect() async throws {
        let client = try makeDirectClient()
        let direct = FakeDirectTransport()
        let connectionGate = ManualGate(), teardownGate = ManualGate()
        direct.connectGate = connectionGate
        direct.disconnectGate = teardownGate
        let livekit = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct }, directSetupMax: .milliseconds(30))
        await media.startDirectPreparation(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        let oldHandler = direct.eventHandler
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
        XCTAssertTrue(accepted)
        XCTAssertEqual(livekit.connectedTokens, ["fallback-token"])
        XCTAssertTrue(direct.audioStopped)
        let before = states
        await oldHandler?(.disconnected(reason: "late"))
        await oldHandler?(.data(Data(#"{"type":"status","status":"connected"}"#.utf8), topic: CallPilotTopic.status))
        XCTAssertEqual(states, before)
        connectionGate.open()
        teardownGate.open()
        await media.stop()
    }

    func testV2StopReleasesLeaseEvenWhenDirectSDKIgnoresCancellation() async throws {
        let client = try makeDirectClient()
        let direct = FakeDirectTransport()
        let gate = ManualGate(), teardown = ManualGate()
        direct.connectGate = gate
        direct.disconnectGate = teardown
        let owner = CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in })
        let media = CallMediaSession(onState: { _ in }, transportFactory: { FakeCallMediaTransport() },
                                     audio: owner, directTransportFactory: { direct })
        await media.startDirectPreparation(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {}) }
        await waitUntil { gate.didStart }
        await media.stop()
        XCTAssertTrue(direct.audioStopped)
        XCTAssertNil(owner.owner)
        gate.open()
        teardown.open()
        let accepted = await answering.value
        XCTAssertFalse(accepted)
    }

    func testV2TURNUnavailableUsesLegacyClaimWithoutSDP() async throws {
        let claimed = expectation(description: "legacy claim")
        MediaMockURLProtocol.requestHandler = { request in
            if request.url?.path.hasSuffix("/ice") == true {
                return Self.httpResponse(for: request, statusCode: 503,
                    json: #"{"error":{"code":"TURN_UNAVAILABLE","message":"Unavailable"}}"#)
            }
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/claim")
            claimed.fulfill()
            return Self.httpResponse(for: request, statusCode: 202,
                json: #"{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"legacy-token","expiresAt":9999}"#)
        }
        let client = try makeClient()
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }), directTransportFactory: { direct })
        await media.startDirectPreparation(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {})
        XCTAssertTrue(accepted)
        await fulfillment(of: [claimed], timeout: 1)
        XCTAssertEqual(direct.prepares, 0)
        XCTAssertEqual(livekit.connectedTokens, ["legacy-token"])
        await media.stop()
    }

    func testV2RejectedICEOfferDoesNotReclaimOrFallback() async throws {
        MediaMockURLProtocol.requestHandler = { request in
            XCTAssertTrue(request.url?.path.hasSuffix("/ice") == true)
            return Self.httpResponse(for: request, statusCode: 409,
                json: #"{"error":{"code":"OFFER_UNAVAILABLE","message":"Ended"}}"#)
        }
        let client = try makeClient()
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }), directTransportFactory: { direct })
        await media.startDirectPreparation(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        let accepted = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {})
        XCTAssertFalse(accepted)
        XCTAssertEqual(direct.prepares, 0)
        XCTAssertFalse(livekit.operations.contains(.connect))
        await media.stop()
    }

    // MARK: - A8 outbound direct

    private func makeOutboundClient(answer: Bool = true, directAttempted: Bool = true, iceUnavailable: Bool = false,
                                    onCreate: ((([String: Any]) -> Void))? = nil,
                                    onFallback: (() -> Void)? = nil) throws -> HostedCloudClient {
        MediaMockURLProtocol.requestHandler = { request in
            let path = request.url?.path ?? ""
            if path == "/v1/calls/ice" {
                if iceUnavailable {
                    return Self.httpResponse(for: request, statusCode: 503,
                        json: #"{"error":{"code":"DIRECT_UNAVAILABLE","message":"Unavailable"}}"#)
                }
                return Self.httpResponse(for: request, statusCode: 200,
                    json: #"{"iceServers":[{"urls":["turn:relay.example:3478"],"username":"test","credential":"test"}],"expiresAt":9999}"#)
            }
            if path == "/v1/calls/call_abcdefghijkl/media/livekit" {
                onFallback?()
                return Self.httpResponse(for: request, statusCode: 202,
                    json: #"{"callId":"call_abcdefghijkl","session":{"livekitUrl":"wss://lk.example.com","token":"fallback-token","expiresAt":9999}}"#)
            }
            if path == "/v1/calls/call_abcdefghijkl" {
                return Self.httpResponse(for: request, statusCode: 200,
                    json: #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"legacy-token","expiresAt":9999}}"#)
            }
            XCTAssertEqual(path, "/v1/calls")
            let body = try Self.directRequestFields(request)
            onCreate?(body)
            let direct = body["sdpOffer"] != nil && directAttempted
            let transports = answer && direct ? #","transports":[{"kind":"webrtc","sdpAnswer":"v=0\r\nanswer"}]"# : ""
            return Self.httpResponse(for: request, statusCode: 202,
                json: #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"#
                    + (direct ? #""ready","directAttempted":true"# : #""pending""#)
                    + #","createdAt":1,"expiresAt":9999"# + transports + "}")
        }
        return try makeClient()
    }

    private func outboundMedia(_ direct: FakeDirectTransport, _ livekit: FakeCallMediaTransport,
                               states: @escaping CallMediaSession.StateHandler = { _ in }) -> CallMediaSession {
        CallMediaSession(onState: states, transportFactory: { livekit },
            audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
            directTransportFactory: { direct })
    }

    func testOutboundDirectAnswersInCreateAndDialsOverDirectAfterMediaReady() async throws {
        var created: [String: Any] = [:]
        let client = try makeOutboundClient(onCreate: { created = $0 }, onFallback: { XCTFail("no fallback") })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = outboundMedia(direct, livekit)
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086", direct: true)
        XCTAssertEqual(created["mediaTransport"] as? String, "webrtc")
        XCTAssertEqual(created["sdpOffer"] as? String, "v=0\r\noffer")
        XCTAssertEqual(direct.activations, 1, "app-dialed direct audio activates without CallKit")
        XCTAssertTrue(livekit.connectedTokens.isEmpty)
        await direct.eventHandler?(.data(Data(#"{"type":"status","status":"media_ready"}"#.utf8), topic: CallPilotTopic.status))
        await waitUntil { direct.packets.contains { String(decoding: $0, as: UTF8.self).contains("10086") } }
        XCTAssertTrue(direct.packets.contains { String(decoding: $0, as: UTF8.self).contains("10086") })
        await media.stop()
    }

    func testOutboundDirectFailureFallsBackOnceToTheSameCallsRoom() async throws {
        var fallbacks = 0
        let client = try makeOutboundClient(onFallback: { fallbacks += 1 })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        direct.failure = true
        var states: [CallState] = []
        let media = outboundMedia(direct, livekit) { states.append($0) }
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086", direct: true)
        await waitUntil { livekit.connectedTokens == ["fallback-token"] }
        XCTAssertEqual(livekit.connectedTokens, ["fallback-token"])
        XCTAssertEqual(fallbacks, 1)
        XCTAssertTrue(direct.audioStopped)
        XCTAssertFalse(states.contains { $0.isTerminal })
        await media.stop()
    }

    func testOutboundWithoutAnEdgeAnswerFallsBackAndAnOldEdgeIsPolled() async throws {
        for (directAttempted, token) in [(true, "fallback-token"), (false, "legacy-token")] {
            let client = try makeOutboundClient(answer: false, directAttempted: directAttempted)
            let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
            let media = outboundMedia(direct, livekit)
            await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086", direct: true)
            await waitUntil { livekit.connectedTokens == [token] }
            XCTAssertEqual(livekit.connectedTokens, [token])
            XCTAssertEqual(direct.activations, 0)
            await media.stop()
        }
    }

    func testOutboundDirectUnavailableKeepsThePlainLiveKitPath() async throws {
        var created: [String: Any] = [:]
        let client = try makeOutboundClient(iceUnavailable: true, onCreate: { created = $0 })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = outboundMedia(direct, livekit)
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086", direct: true)
        await waitUntil { livekit.connectedTokens == ["legacy-token"] }
        XCTAssertEqual(direct.prepares, 0)
        XCTAssertNil(created["sdpOffer"])
        XCTAssertEqual(livekit.connectedTokens, ["legacy-token"])
        await media.stop()
    }

    func testOutboundEdgeWithoutPhoneAudioAsksForTheFallbackBeforeDialing() async throws {
        var fallbacks = 0
        let client = try makeOutboundClient(onFallback: { fallbacks += 1 })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        let media = outboundMedia(direct, livekit)
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086", direct: true)
        await direct.eventHandler?(.data(Data(#"{"type":"media_failed","reason":"no_phone_audio"}"#.utf8),
                                           topic: CallPilotTopic.status))
        await waitUntil { livekit.connectedTokens == ["fallback-token"] }
        XCTAssertEqual(livekit.connectedTokens, ["fallback-token"])
        XCTAssertEqual(fallbacks, 1)
        await media.stop()
    }

    func testOutboundDirectLossAfterDialEndsTheCallInsteadOfSwitching() async throws {
        let client = try makeOutboundClient(onFallback: { XCTFail("a dialed call cannot switch transports") })
        let direct = FakeDirectTransport(), livekit = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = outboundMedia(direct, livekit) { states.append($0) }
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086", direct: true)
        await direct.eventHandler?(.data(Data(#"{"type":"status","status":"media_ready"}"#.utf8), topic: CallPilotTopic.status))
        await waitUntil { !direct.packets.isEmpty }
        await direct.eventHandler?(.disconnected(reason: "ICE failed while ringing"))
        XCTAssertTrue(states.last?.isTerminal == true)
        XCTAssertTrue(livekit.connectedTokens.isEmpty)
        await media.stop()
    }

    private func makeDirectClient(includeAnswer: Bool = true, iceUnavailable: Bool = false, rejectClaim: Bool = false,
                                  onFallback: (() -> Void)? = nil, onClaim: ((Bool) -> Void)? = nil,
                                  onSupplement: (() -> Void)? = nil, onEnd: (() -> Void)? = nil,
                                  onFallbackBody: (([String: Any]) throws -> Void)? = nil,
                                  onEndBody: (([String: Any]) throws -> Void)? = nil) throws -> HostedCloudClient {
        var claims = 0
        var supplements = 0
        MediaMockURLProtocol.requestHandler = { request in
            let path = request.url?.path ?? ""
            if path.hasSuffix("/ice") {
                if iceUnavailable {
                    return Self.httpResponse(for: request, statusCode: 503,
                        json: #"{"error":{"code":"TURN_UNAVAILABLE","message":"Unavailable"}}"#)
                }
                return Self.httpResponse(for: request, statusCode: 200,
                    json: #"{"iceServers":[{"urls":["turn:relay.example:3478"],"username":"test","credential":"test"}],"expiresAt":9999}"#)
            }
            if path.hasSuffix("/media/livekit") {
                try onFallbackBody?(Self.directRequestFields(request))
                onFallback?()
                return Self.httpResponse(for: request, statusCode: 202,
                    json: #"{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"fallback-token","expiresAt":9999}"#)
            }
            let body = try Self.directRequestFields(request)
            if path.hasSuffix("/media/end") {
                try onEndBody?(body)
                XCTAssertEqual(body["claimId"] as? String, "claim_abcdefghijkl")
                onEnd?()
                return Self.httpResponse(for: request, statusCode: 202,
                    json: #"{"accepted":true,"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl"}"#)
            }
            if path.hasSuffix("/media/sdp") {
                onSupplement?()
                supplements += 1
                XCTAssertEqual(supplements, 1)
                XCTAssertEqual(body["claimId"] as? String, "claim_abcdefghijkl")
                XCTAssertNotNil(body["sdpOffer"])
            } else {
                XCTAssertEqual(path, "/v1/inbound-offers/claim")
                claims += 1
                XCTAssertEqual(claims, 1, "An accepted claim must never be retried")
                XCTAssertEqual(body["mediaTransport"] as? String, "webrtc")
                onClaim?(body["sdpOffer"] != nil)
                if rejectClaim {
                    return Self.httpResponse(for: request, statusCode: 409,
                        json: #"{"error":{"code":"OFFER_UNAVAILABLE","message":"Another device won"}}"#)
                }
                if includeAnswer && body["sdpOffer"] == nil {
                    return Self.httpResponse(for: request, statusCode: 202,
                        json: #"{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"unused-token","expiresAt":9999,"directAttempted":true,"sdpPending":true,"transports":[{"kind":"livekit","url":"wss://lk.example.com","token":"unused-token"}]}"#)
                }
            }
            if !includeAnswer {
                return Self.httpResponse(for: request, statusCode: 202,
                    json: #"{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"unused-token","expiresAt":9999,"directAttempted":true,"transports":[{"kind":"livekit","url":"wss://lk.example.com","token":"unused-token"}]}"#)
            }
            return Self.httpResponse(for: request, statusCode: 202,
                json: #"{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"unused-token","expiresAt":9999,"directAttempted":true,"transports":[{"kind":"webrtc","sdpAnswer":"v=0\\r\\nanswer"},{"kind":"livekit","url":"wss://lk.example.com","token":"unused-token"}]}"#)
        }
        return try makeClient()
    }

    nonisolated private static func directRequestFields(_ request: URLRequest) throws -> [String: Any] {
        var data = request.httpBody ?? Data()
        if request.httpBody == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while true {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count < 0 { throw try XCTUnwrap(stream.streamError) }
                if count == 0 { break }
                data.append(buffer, count: count)
            }
        }
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testColdClaimAfterEightSecondsDoesNotWaitForStuckSilentTeardown() async throws {
        try await assertDetachedSilentTeardown(stopBeforeDrain: false)
    }

    func testTerminationHoldsAudioLeaseUntilDetachedSilentTeardownFinishes() async throws {
        try await assertDetachedSilentTeardown(stopBeforeDrain: true)
    }

    func testReadyPreconnectRejectedAsColdDoesNotWaitForStuckDisconnect() async throws {
        try await assertDetachedSilentTeardown(stopBeforeDrain: false, readyBeforeAnswer: true)
    }

    private func assertDetachedSilentTeardown(stopBeforeDrain: Bool, readyBeforeAnswer: Bool = false) async throws {
        let claimReceived = expectation(description: "claim reaches server before silent teardown is released")
        let client = try makePreparedClient(mode: "cold", onClaim: { claimReceived.fulfill() })
        let silent = FakeCallMediaTransport(), cold = FakeCallMediaTransport()
        let connectGate = ManualGate(), disconnectGate = ManualGate()
        let finalDisconnectGate = ManualGate()
        silent.silentConnectGate = connectGate
        silent.disconnectGate = disconnectGate
        if stopBeforeDrain { silent.thirdDisconnectGate = finalDisconnectGate }
        silent.ignoreSilentCancellation = true
        defer { connectGate.open(); disconnectGate.open(); finalDisconnectGate.open() }
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        var factoryCount = 0
        var states: [CallState] = []
        var claimedAt: ContinuousClock.Instant?
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: {
            factoryCount += 1
            return factoryCount == 1 ? silent : cold
        }, audio: audio)
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { connectGate.didStart }
        if readyBeforeAnswer {
            connectGate.open()
            await preparing.value
        }
        let owner = audio.owner
        XCTAssertNotNil(owner)
        let lateHandler = silent.eventHandler
        let answeredAt = ContinuousClock.now
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { claimedAt = .now }, onConnected: {}) }
        await fulfillment(of: [claimReceived], timeout: readyBeforeAnswer ? 1 : 9)
        await waitUntil { cold.operations.contains(.connect) }
        XCTAssertEqual(factoryCount, 2)
        XCTAssertEqual(cold.operations.filter { $0 == .connect }.count, 1)
        XCTAssertNotNil(claimedAt)
        if let claimedAt {
            if !readyBeforeAnswer {
                XCTAssertGreaterThanOrEqual(answeredAt.duration(to: claimedAt), .seconds(8))
            }
            XCTAssertLessThan(answeredAt.duration(to: claimedAt), readyBeforeAnswer ? .seconds(1) : .seconds(9))
        }
        await waitUntil { disconnectGate.didStart }
        XCTAssertFalse(silent.operations.contains(.disconnect))
        XCTAssertNil(silent.eventHandler)
        XCTAssertNil(field(media, "silentTransport", as: FakeCallMediaTransport.self))
        XCTAssertNil(field(media, "preparation", as: InboundPreparation.self))
        XCTAssertEqual(field(media, "preconnectReady", as: Bool.self), false)
        XCTAssertTrue(field(media, "transport", as: FakeCallMediaTransport.self) === cold)
        let events: [CallMediaTransportEvent] = [
            .permissionsReady(true), .tracksChanged,
            .data(Data(#"{"type":"status","status":"connected","clientTimingVersion":1}"#.utf8),
                  topic: CallPilotTopic.status),
            .timing(.downlinkFirstFrame, at: CallMonotonic.now()),
            .disconnected(reason: "late_silent_disconnect"),
        ]
        let before = states
        for event in events { await lateHandler?(event) }
        XCTAssertEqual(states, before)
        XCTAssertEqual(field(media, "permissionReady", as: Bool.self), false)
        XCTAssertNil(timingMarks(media)[.downlinkFirstFrame])
        XCTAssertEqual(device.modes, [.callKit])

        var stopped = false
        let stopping: Task<Void, Never>? = stopBeforeDrain ? Task {
            await media.stop()
            stopped = true
        } : nil
        let next = UUID()
        var nextAcquired = false
        var acquiring: Task<Void, Error>?
        if stopBeforeDrain {
            await waitUntil { cold.operations.contains(.disconnect) }
            acquiring = Task {
                try await audio.acquire(next, mode: .standalone)
                nextAcquired = true
            }
            await Task.yield()
            XCTAssertFalse(stopped)
            XCTAssertFalse(nextAcquired)
            XCTAssertEqual(audio.owner, owner)
        }
        if !stopBeforeDrain && !readyBeforeAnswer {
            // A cancelled SDK connect can succeed before its stuck disconnect returns.
            connectGate.open()
            await waitUntil { silent.silentConnectReturnCount == 1 }
            XCTAssertNil(timingMarks(media)[.preconnectReady])
            XCTAssertTrue(field(media, "transport", as: FakeCallMediaTransport.self) === cold)
        }
        // Even a completed disconnect must not release the lease while connect is stuck.
        disconnectGate.open()
        await waitUntil { silent.operations.contains(.disconnect) }
        if stopBeforeDrain {
            XCTAssertFalse(stopped)
            XCTAssertFalse(nextAcquired)
            XCTAssertEqual(audio.owner, owner)
        }
        connectGate.open()
        await preparing.value
        let joined = await answering.value
        if stopBeforeDrain {
            // The silent task has settled, but the background reaper still owns work.
            await waitUntil { finalDisconnectGate.didStart }
            XCTAssertFalse(stopped)
            XCTAssertFalse(nextAcquired)
            XCTAssertEqual(audio.owner, owner)
            finalDisconnectGate.open()
        }
        await stopping?.value
        try await acquiring?.value
        if stopBeforeDrain {
            XCTAssertEqual(audio.owner, next)
            XCTAssertTrue(nextAcquired)
            await audio.release(next, after: {}).value
        } else {
            XCTAssertTrue(joined)
            for event in events { await lateHandler?(event) }
            XCTAssertEqual(states, before)
            XCTAssertTrue(field(media, "transport", as: FakeCallMediaTransport.self) === cold)
            XCTAssertNil(field(media, "preparation", as: InboundPreparation.self))
            XCTAssertEqual(field(media, "preconnectReady", as: Bool.self), false)
            if !readyBeforeAnswer { XCTAssertNil(timingMarks(media)[.preconnectReady]) }
            XCTAssertFalse(cold.operations.contains(.disconnect))
            await media.stop()
        }
        XCTAssertGreaterThanOrEqual(silent.operations.filter { $0 == .disconnect }.count, 2)
        XCTAssertEqual(cold.operations.filter { $0 == .connect }.count, 1)
        XCTAssertEqual(silent.microphoneEnableCount, 0)
        XCTAssertTrue(silent.subscriptions.isEmpty)
        XCTAssertNil(audio.owner)
    }

    func testTerminalResetAndDroppingMediaKeepHangupAndAudioCleanupAheadOfNextCall() async throws {
        let client = try makeTakeoverClient(offerId: "offer_previouscall")
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let previousTransport = FakeCallMediaTransport()
        let publishGate = PublishGate()
        previousTransport.hangupPublishGate = publishGate
        previousTransport.acknowledgeHangup = true
        var attempts = CallAttemptStateMachine()
        let previousAttempt = attempts.begin(with: .waitingMedia(label: "previous"))
        var media: CallMediaSession? = CallMediaSession(onState: {
            _ = attempts.transition(to: $0, for: previousAttempt)
        }, transportFactory: { previousTransport }, audio: audio, hangupBudget: .seconds(5))
        let previousJoined = await media?.startTakeover(client: client, offerId: "offer_previouscall", usesCallKit: true,
                                                       onConnected: {})
        XCTAssertEqual(previousJoined, true)
        let previousOwner = try XCTUnwrap(audio.owner)
        let packet = try XCTUnwrap(media?.requestHangup())
        XCTAssertTrue(attempts.state.isTerminal)
        XCTAssertTrue(attempts.resetTerminal())
        weak var drainingMedia = media
        media = nil
        XCTAssertNotNil(drainingMedia)
        XCTAssertEqual(attempts.state, .idle)

        await waitUntil { publishGate.didStart }
        let incomingClient = try makeTakeoverClient(offerId: "offer_incomingcall")
        let incomingAttempt = attempts.begin(with: .preparing(label: "incoming"))
        let incomingTransport = FakeCallMediaTransport()
        let incoming = CallMediaSession(onState: {
            _ = attempts.transition(to: $0, for: incomingAttempt)
        }, transportFactory: { incomingTransport }, audio: audio)
        let answering = Task {
            await incoming.startTakeover(client: incomingClient, offerId: "offer_incomingcall", usesCallKit: true,
                                         onConnected: {})
        }
        await waitUntil { self.timingMarks(incoming)[.audioLeaseStart] != nil }
        XCTAssertEqual(audio.owner, previousOwner)
        XCTAssertEqual(device.modes, [.callKit])
        XCTAssertFalse(previousTransport.operations.contains(.disconnect))
        XCTAssertTrue(incomingTransport.operations.isEmpty)

        publishGate.open()
        let joined = await answering.value
        XCTAssertTrue(joined)
        XCTAssertEqual(previousTransport.hangupPackets, [packet])
        let publishIndex = try XCTUnwrap(previousTransport.operations.firstIndex(of: .publish(packet)))
        let disconnectIndex = try XCTUnwrap(previousTransport.operations.firstIndex(of: .disconnect))
        XCTAssertLessThan(publishIndex, disconnectIndex)
        XCTAssertNotEqual(audio.owner, previousOwner)
        XCTAssertEqual(device.modes, [.callKit, .callKit])
        XCTAssertTrue(attempts.isCurrent(incomingAttempt))
        XCTAssertTrue(attempts.state.isActive)
        await incoming.stop()
        XCTAssertNil(audio.owner)
    }

    func testAnswerWhileAudioLeaseWaitsCancelsSilentStateAndConnectsClaimSessionOnce() async throws {
        try await assertColdTakeover(at: .audioLease)
    }

    func testAnswerWhilePrepareWaitsClaimsColdBeforePrepareReturns() async throws {
        try await assertColdClaimBeforePrepareReturns(failPrepare: false)
    }

    func testAnswerKeepsWaitingBeyondThreeSecondsAndReusesSilentConnection() async throws {
        try await assertWaitingTakeoverReuses(after: .milliseconds(3200))
    }

    func testAnswerWhileSilentConnectWaitsThenReusesOneConnection() async throws {
        try await assertWaitingTakeoverReuses(after: .milliseconds(50))
    }

    func testAnswerWhilePrepareWaitsImmediatelyCancelsSilentSetupAndFallsBackToCold() async throws {
        try await assertColdTakeover(at: .prepare)
    }

    func testAnswerWaitsEightSecondsBeforeColdFallbackAndFencesLateEvents() async throws {
        try await assertColdTakeover(at: .connect)
    }

    func testReadyPreconnectRejectedAsColdFencesOldConnectionEventsAndAck() async throws {
        try await assertColdTakeover(at: .ready)
    }

    func testSilentConnectionCreatedDuringCancellationDisconnectsWithoutPublishingState() async throws {
        let client = try makePreparedClient(mode: "cold")
        let transport = FakeCallMediaTransport()
        weak var cancellingMedia: CallMediaSession?
        let media = CallMediaSession(onState: { _ in }, transportFactory: {
            cancellingMedia?.cancelSilently()
            return transport
        })
        cancellingMedia = media
        await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        await media.stop()
        XCTAssertEqual(transport.silentConnectCount, 0)
        XCTAssertTrue(transport.operations.contains(.disconnect))
        XCTAssertNil(field(media, "transport", as: FakeCallMediaTransport.self))
        XCTAssertNil(field(media, "preparation", as: InboundPreparation.self))
        XCTAssertEqual(field(media, "preconnectReady", as: Bool.self), false)
        XCTAssertNil(timingMarks(media)[.preconnectReady])
    }

    func testPrepareStartsWhilePreviousAudioCleanupIsStillDraining() async throws {
        let responseGate = ResponseGate()
        let client = try makePreparedClient(mode: "cold", prepareGate: responseGate)
        let audio = CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in XCTFail("cleanup") })
        let previous = UUID()
        try await audio.acquire(previous, mode: .callKit)
        let drain = ConnectGate()
        let cleanup = audio.release(previous) { await drain.wait() }
        await waitUntil { drain.didStart }
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { responseGate.didStart }
        XCTAssertEqual(transport.silentConnectCount, 0)
        responseGate.open()
        await waitUntil { self.timingMarks(media)[.prepareResponse] != nil }
        XCTAssertEqual(transport.silentConnectCount, 0)
        drain.open()
        await cleanup.value
        await preparing.value
        let marks = timingMarks(media)
        XCTAssertNotNil(marks[.prepareStart])
        XCTAssertNotNil(marks[.preconnectReady])
        let namedMarks = Dictionary(uniqueKeysWithValues: marks.map { ($0.key.rawValue, $0.value) })
        let leaseStart = try XCTUnwrap(namedMarks["audio_lease_start"])
        let leaseEnd = try XCTUnwrap(namedMarks["audio_lease_end"])
        XCTAssertLessThanOrEqual(try XCTUnwrap(marks[.prepareStart]), leaseStart)
        XCTAssertLessThanOrEqual(leaseStart, leaseEnd)
        XCTAssertLessThan(try XCTUnwrap(marks[.prepareResponse]), leaseEnd)
        XCTAssertLessThanOrEqual(leaseEnd, try XCTUnwrap(marks[.preconnectStart]))
        await media.stop()
    }

    private enum SilentAnswerStage { case audioLease, prepare, connect, ready }

    private func assertWaitingTakeoverReuses(after delay: Duration) async throws {
        let client = try makePreparedClient(mode: "reuse")
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let transport = FakeCallMediaTransport()
        transport.connectGate = ConnectGate()
        var factoryCount = 0
        var claimed = false
        let media = CallMediaSession(onState: { _ in }, transportFactory: {
            factoryCount += 1
            return transport
        }, audio: audio)
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { transport.connectGate?.didStart == true }
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true, onClaimed: {
                claimed = true
                try media.prepareCallKitAnswer()
            }, onConnected: {}) }
        await waitUntil { self.field(media, "claimStarted", as: Bool.self) == true }
        try await Task.sleep(for: delay)
        XCTAssertFalse(claimed)
        XCTAssertNil(timingMarks(media)[.claimStart])
        XCTAssertNil(timingMarks(media)[.coldFallback])
        XCTAssertEqual(device.configureCount, 0)
        XCTAssertEqual(device.activateCount, 0)
        transport.connectGate?.open()
        let joined = await answering.value
        await preparing.value
        XCTAssertTrue(joined)
        XCTAssertTrue(claimed)
        XCTAssertEqual(factoryCount, 1)
        XCTAssertEqual(transport.silentConnectCount, 1)
        XCTAssertFalse(transport.operations.contains(.connect))
        XCTAssertFalse(transport.operations.contains(.disconnect))
        XCTAssertEqual(device.configureCount, 1)
        XCTAssertEqual(device.activateCount, 0)
        XCTAssertNotNil(timingMarks(media)[.transportAdopted])
        XCTAssertNil(timingMarks(media)[.coldFallback])
        XCTAssertLessThanOrEqual(try XCTUnwrap(timingMarks(media)[.preconnectReady]),
                                 try XCTUnwrap(timingMarks(media)[.claimStart]))
        await media.stop()
        XCTAssertNil(audio.owner)
    }

    func testSilentFailureDuringAnswerWaitFallsBackWithoutWaitingForDeadline() async throws {
        for disconnectEvent in [false, true] {
            let client = try makePreparedClient(mode: "cold")
            let silent = FakeCallMediaTransport(), cold = FakeCallMediaTransport()
            silent.connectGate = ConnectGate()
            let disconnectGate = ManualGate()
            silent.disconnectGate = disconnectGate
            defer { disconnectGate.open() }
            var factoryCount = 0
            let media = CallMediaSession(onState: { _ in }, transportFactory: {
                factoryCount += 1
                if factoryCount == 2 { XCTAssertNil(silent.eventHandler) }
                return factoryCount == 1 ? silent : cold
            })
            let preparing = Task { await media.startSilentPreconnect(client: client,
                offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
            await waitUntil { silent.connectGate?.didStart == true }
            let answering = Task { await media.startTakeover(client: client,
                offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {}) }
            await waitUntil { self.field(media, "claimStarted", as: Bool.self) == true }
            try await Task.sleep(for: .milliseconds(50))
            XCTAssertNil(timingMarks(media)[.claimStart])
            let failedAt = ContinuousClock.now
            if disconnectEvent {
                await silent.emit(.disconnected(reason: "connection_failed"))
                // The SDK may return from connect after delivering a disconnect.
                silent.connectGate?.open()
            } else {
                silent.silentConnectError = URLError(.cannotConnectToHost)
                silent.connectGate?.open()
            }
            await waitUntil { self.timingMarks(media)[.claimStart] != nil }
            XCTAssertLessThan(failedAt.duration(to: .now), .seconds(1))
            XCTAssertFalse(silent.operations.contains(.disconnect))
            disconnectGate.open()
            let joined = await answering.value
            await preparing.value
            XCTAssertTrue(joined)
            XCTAssertLessThan(failedAt.duration(to: .now), .seconds(1))
            XCTAssertNotNil(timingMarks(media)[.coldFallback])
            XCTAssertNil(timingMarks(media)[.preconnectReady])
            XCTAssertEqual(factoryCount, 2)
            XCTAssertEqual(cold.operations.filter { $0 == .connect }.count, 1)
            await media.stop()
        }
    }

    func testLatePrepareFailureDoesNotDelayOrFailColdClaim() async throws {
        try await assertColdClaimBeforePrepareReturns(failPrepare: true)
    }

    private func assertColdClaimBeforePrepareReturns(failPrepare: Bool) async throws {
        let gate = ResponseGate()
        defer { gate.open() }
        let claimReceived = expectation(description: "cold claim before prepare returns")
        let client = try makePreparedClient(mode: "cold", prepareGate: gate, failPrepare: failPrepare,
                                           onClaim: { claimReceived.fulfill() })
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { gate.didStart }
        let answeredAt = ContinuousClock.now
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {}) }
        await fulfillment(of: [claimReceived], timeout: 1)
        await waitUntil { self.timingMarks(media)[.claimStart] != nil }
        XCTAssertLessThan(answeredAt.duration(to: .now), .seconds(1))
        XCTAssertNil(timingMarks(media)[.prepareResponse])
        XCTAssertNil(timingMarks(media)[.preconnectStart])
        gate.open()
        let joined = await answering.value
        await preparing.value
        XCTAssertTrue(joined)
        XCTAssertNotNil(timingMarks(media)[.coldFallback])
        XCTAssertEqual(transport.silentConnectCount, 0)
        XCTAssertEqual(transport.operations.filter { $0 == .connect }.count, 1)
        await media.stop()
    }

    func testHangupOrCancellationDuringAnswerWaitStopsWithoutClaiming() async throws {
        for cancellation in ["hangup", "session", "task"] {
            let client = try makePreparedClient(mode: "reuse")
            let transport = FakeCallMediaTransport()
            transport.connectGate = ConnectGate()
            transport.ignoreSilentCancellation = true
            transport.acknowledgeHangup = true
            let device = TestCallAudioDevice()
            let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
            let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio,
                                        hangupBudget: .milliseconds(100))
            let preparing = Task { await media.startSilentPreconnect(client: client,
                offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
            await waitUntil { transport.connectGate?.didStart == true }
            let answering = Task { await media.startTakeover(client: client,
                offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {}) }
            await waitUntil { self.field(media, "claimStarted", as: Bool.self) == true }
            try await Task.sleep(for: .milliseconds(50))
            let cancelledAt = ContinuousClock.now
            switch cancellation {
            case "hangup": media.requestHangup()
            case "session": media.cancelSilently()
            default: answering.cancel()
            }
            await waitUntil { transport.operations.contains(.disconnect) }
            let joined = await answering.value
            await preparing.value
            await media.stop()
            XCTAssertFalse(joined)
            XCTAssertLessThan(cancelledAt.duration(to: .now), .seconds(1))
            XCTAssertNil(timingMarks(media)[.claimStart])
            XCTAssertNil(timingMarks(media)[.preconnectReady])
            XCTAssertNil(media.preparedSession)
            XCTAssertNil(audio.owner)
            XCTAssertEqual(device.configureCount, 0)
            XCTAssertEqual(device.activateCount, 0)
        }
    }

    private func assertColdTakeover(at stage: SilentAnswerStage) async throws {
        let responseGate = stage == .prepare ? ResponseGate() : nil
        let client = try makePreparedClient(mode: "cold", prepareGate: responseGate)
        defer { responseGate?.open() }
        let audio = CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in XCTFail("cleanup") })
        let drain = ConnectGate()
        var cleanup: Task<Void, Never>?
        if stage == .audioLease {
            let previous = UUID()
            try await audio.acquire(previous, mode: .callKit)
            cleanup = audio.release(previous) { await drain.wait() }
            await waitUntil { drain.didStart }
        }
        let silent = FakeCallMediaTransport(), cold = FakeCallMediaTransport()
        cold.connectGate = ConnectGate()
        if stage == .connect { silent.connectGate = ConnectGate() }
        silent.ignoreSilentCancellation = true
        let expectsSilent = stage == .connect || stage == .ready
        var factoryCount = 0
        var states: [CallState] = []
        var silentDisconnects = 0
        var connectedCount = 0
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: {
            factoryCount += 1
            return expectsSilent && factoryCount == 1 ? silent : cold
        }, audio: audio, hangupBudget: .seconds(2))
        media.onSilentDisconnect = { silentDisconnects += 1 }
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        switch stage {
        case .audioLease:
            await waitUntil { self.timingMarks(media)[.prepareResponse] != nil }
            XCTAssertEqual(factoryCount, 0)
        case .prepare:
            await waitUntil { responseGate?.didStart == true }
        case .connect:
            await waitUntil { silent.connectGate?.didStart == true }
        case .ready:
            await preparing.value
        }
        let lateHandler = silent.eventHandler
        let answeredAt = ContinuousClock.now
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { try media.prepareCallKitAnswer() }, onConnected: { connectedCount += 1 }) }
        await waitUntil(timeout: stage == .connect ? .seconds(9) : .seconds(1)) {
            self.timingMarks(media)[.coldFallback] != nil
        }
        if stage == .connect {
            XCTAssertGreaterThanOrEqual(answeredAt.duration(to: .now), .seconds(8))
            XCTAssertLessThan(answeredAt.duration(to: .now), .seconds(9))
        } else {
            XCTAssertLessThan(answeredAt.duration(to: .now), .seconds(1))
        }
        // Let cancellation reach dependencies which deliberately finish late.
        await Task.yield()
        responseGate?.open()
        if stage == .audioLease { drain.open() }
        await cleanup?.value
        await waitUntil { cold.connectGate?.didStart == true }
        let before = states
        let events: [CallMediaTransportEvent] = [
            .permissionsReady(true), .tracksChanged,
            .data(Data(#"{"type":"status","status":"connected","clientTimingVersion":1}"#.utf8),
                  topic: CallPilotTopic.status),
            .timing(.downlinkFirstFrame, at: CallMonotonic.now()),
            .disconnected(reason: "stale_silent_connection"),
        ]
        // Deliver already-queued callbacks while the winner's room is still connecting.
        for event in events { await lateHandler?(event) }
        XCTAssertEqual(states, before)
        cold.connectGate?.open()
        let joined = await answering.value
        await preparing.value
        XCTAssertTrue(joined)
        XCTAssertEqual(factoryCount, expectsSilent ? 2 : 1)
        XCTAssertEqual(cold.connectedURLs, ["wss://winner.example.com"])
        XCTAssertEqual(cold.connectedTokens, ["winner-token"])
        XCTAssertEqual(cold.operations.filter { $0 == .connect }.count, 1)
        XCTAssertEqual(silent.silentConnectCount, expectsSilent ? 1 : 0)
        XCTAssertEqual(silent.operations.contains(.disconnect), expectsSilent)
        XCTAssertTrue(field(media, "transport", as: FakeCallMediaTransport.self) === cold)
        XCTAssertEqual(field(media, "preconnectReady", as: Bool.self), false)
        XCTAssertNil(field(media, "preparation", as: InboundPreparation.self))
        if stage != .ready { XCTAssertNil(timingMarks(media)[.preconnectReady]) }

        for event in events { await lateHandler?(event) }
        XCTAssertEqual(states, before)
        XCTAssertEqual(connectedCount, 0)
        XCTAssertEqual(silentDisconnects, 0)
        XCTAssertEqual(field(media, "permissionReady", as: Bool.self), false)
        XCTAssertEqual(field(media, "supportsClientTiming", as: Bool.self), false)
        XCTAssertNil(timingMarks(media)[.downlinkFirstFrame])
        XCTAssertTrue(silent.subscriptions.isEmpty)
        XCTAssertEqual(silent.microphoneEnableCount, 0)
        XCTAssertTrue(field(media, "transport", as: FakeCallMediaTransport.self) === cold)
        await media.sendDTMF("2")
        XCTAssertEqual(cold.controlPackets.count, 1)
        XCTAssertTrue(silent.controlPackets.isEmpty)

        let packet = media.requestHangup()
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: packet) as? [String: String])
        let ack = try JSONSerialization.data(withJSONObject: [
            "type": "hangup.ack", "commandId": try XCTUnwrap(fields["commandId"]),
        ])
        await lateHandler?(.data(ack, topic: CallPilotTopic.status))
        XCTAssertEqual(field(media, "hangupAcknowledged", as: Bool.self), false)
        await cold.emit(.data(ack, topic: CallPilotTopic.status))
        XCTAssertEqual(field(media, "hangupAcknowledged", as: Bool.self), true)
        await media.stop()
        XCTAssertNil(audio.owner)
    }

    // Inspect private lifecycle state without widening the production API for tests.
    private func field<T>(_ object: Any, _ name: String, as type: T.Type) -> T? {
        Mirror(reflecting: object).children.first { $0.label == name }?.value as? T
    }

    private func timingMarks(_ media: CallMediaSession) -> [CallTimingMark: TimeInterval] {
        field(media.timing, "marks", as: [CallTimingMark: TimeInterval].self) ?? [:]
    }

    func testSilentPreconnectReusesOneLeaseAndRequiresAllThreeMediaGates() async throws {
        for permissionFirst in [false, true] {
            let client = try makePreparedClient(mode: "reuse")
            let transport = FakeCallMediaTransport()
            let device = TestCallAudioDevice()
            let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
            let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
            await media.startSilentPreconnect(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
            XCTAssertEqual(transport.silentConnectCount, 1)
            XCTAssertEqual(transport.microphoneEnableCount, 0)
            XCTAssertTrue(transport.subscriptions.isEmpty)
            XCTAssertEqual(device.modes, [.callKit])
            XCTAssertEqual(device.configureCount, 0)
            XCTAssertEqual(device.activateCount, 0)
            try media.activateCallKitAudio()
            XCTAssertEqual(device.activateCount, 0)
            if permissionFirst { await transport.emit(.permissionsReady(true)) }
            XCTAssertEqual(transport.microphoneEnableCount, 0)
            let claimed = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
                onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
            XCTAssertTrue(claimed)
            if !permissionFirst { XCTAssertEqual(transport.microphoneEnableCount, 0) }
            try media.activateCallKitAudio()
            if !permissionFirst {
                await Task.yield()
                XCTAssertEqual(transport.microphoneEnableCount, 0)
                XCTAssertTrue(transport.subscriptions.isEmpty)
                await transport.emit(.permissionsReady(true))
            }
            await waitUntil { transport.microphoneEnableCount == 1 }
            XCTAssertEqual(transport.subscriptions, ["edgepart_abcdefghijkl"])
            XCTAssertEqual(device.modes, [.callKit])
            XCTAssertFalse(transport.operations.contains(.connect))
            XCTAssertTrue(field(media, "transport", as: FakeCallMediaTransport.self) === transport)
            XCTAssertEqual(field(media, "preconnectReady", as: Bool.self), true)
            XCTAssertNotNil(field(media, "preparation", as: InboundPreparation.self))
            XCTAssertFalse(transport.operations.contains(.disconnect))
            let duplicate = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {})
            XCTAssertFalse(duplicate)
            await media.stop()
            XCTAssertNil(audio.owner)
            XCTAssertTrue(transport.hangupPackets.isEmpty)
        }
    }

    func testColdFallbackDetachesPreparedTransportBeforeConnectingAndKeepsLease() async throws {
        let client = try makePreparedClient(mode: "cold")
        let silent = FakeCallMediaTransport(), cold = FakeCallMediaTransport()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        var count = 0
        let media = CallMediaSession(onState: { _ in }, transportFactory: {
            count += 1
            if count == 2 { XCTAssertNil(silent.eventHandler) }
            return count == 1 ? silent : cold
        }, audio: audio)
        await media.startSilentPreconnect(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        _ = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
        XCTAssertEqual(count, 2)
        XCTAssertEqual(device.modes, [.callKit])
        XCTAssertTrue(silent.subscriptions.isEmpty)
        XCTAssertEqual(silent.microphoneEnableCount, 0)
        try media.activateCallKitAudio()
        await waitUntil { cold.microphoneEnableCount == 1 }
        await media.stop()
    }

    func testAnswerDuringSilentConnectCancelsAndEventuallyDrainsWithSingleColdClaim() async throws {
        let client = try makePreparedClient(mode: "cold")
        let silent = FakeCallMediaTransport(), cold = FakeCallMediaTransport()
        silent.connectGate = ConnectGate()
        var count = 0
        let media = CallMediaSession(onState: { _ in }, transportFactory: {
            count += 1
            return count == 1 ? silent : cold
        })
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { silent.connectGate?.didStart == true }
        _ = await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
        await preparing.value
        XCTAssertEqual(count, 2)
        XCTAssertEqual(silent.microphoneEnableCount, 0)
        XCTAssertTrue(silent.operations.contains(.disconnect))
        await media.stop()
    }

    func testSilentTerminationNeverPublishesHangupAndReleasesAudio() async throws {
        let client = try makePreparedClient(mode: "reuse")
        let transport = FakeCallMediaTransport()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
        await media.startSilentPreconnect(client: client, offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999))
        await transport.emit(.disconnected(reason: "removed"))
        await media.stop()
        XCTAssertNil(audio.owner)
        XCTAssertEqual(device.activateCount, 0)
        XCTAssertTrue(transport.controlPackets.isEmpty)
        XCTAssertEqual(transport.microphoneEnableCount, 0)
    }

    func testSilentCancellationRegistersBeforeAnotherCallOrQueuedPrepareCanStart() async throws {
        let client = try makePreparedClient(mode: "reuse")
        let transport = FakeCallMediaTransport()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
        let queued = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        media.cancelSilently()
        await queued.value
        await media.stop()
        XCTAssertTrue(device.modes.isEmpty)
        XCTAssertEqual(transport.silentConnectCount, 0)
        XCTAssertTrue(transport.controlPackets.isEmpty)
    }

    private func makePreparedClient(mode: String, prepareGate: ResponseGate? = nil,
                                    failPrepare: Bool = false, onClaim: (() -> Void)? = nil) throws -> HostedCloudClient {
        MediaMockURLProtocol.requestHandler = { request in
            if request.url?.path == "/v1/inbound-offers/prepare" {
                prepareGate?.wait()
                if failPrepare { throw URLError(.cannotConnectToHost) }
                return Self.httpResponse(for: request, statusCode: 200,
                    json: #"{"prepareId":"prepare_abcdefghijkl","roomName":"callpilot_abcdefghijkl","participantIdentity":"web_abcdefghijkl","url":"wss://lk.example.com","token":"restricted-token","expiresAt":9999}"#)
            }
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/claim")
            onClaim?()
            return Self.httpResponse(for: request, statusCode: 202,
                json: "{\"claimId\":\"claim_abcdefghijkl\",\"offerId\":\"offer_abcdefghijkl\",\"roomName\":\"callpilot_abcdefghijkl\",\"participantIdentity\":\"web_abcdefghijkl\",\"edgeIdentity\":\"edgepart_abcdefghijkl\",\"url\":\"wss://winner.example.com\",\"token\":\"winner-token\",\"expiresAt\":9999,\"mode\":\"\(mode)\"}")
        }
        return try makeClient()
    }
    func testTelemetryWaitsForEdgeCapabilityAndThenFlushesCachedMarks() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "1")
        try await Task.sleep(for: .milliseconds(60))
        XCTAssertFalse(transport.operations.contains { operation in
            guard case let .publish(data) = operation,
                  let fields = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
            return fields["type"] as? String == "client_timing"
        })
        await transport.emit(.data(Data(#"{"type":"status","status":"connected","clientTimingVersion":1}"#.utf8),
                                   topic: CallPilotTopic.status))
        await waitUntil {
            transport.operations.contains { operation in
                guard case let .publish(data) = operation,
                      let fields = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
                return fields["type"] as? String == "client_timing"
            }
        }
        await media.stop()
    }

    func testLocalHangupSurvivesCallKitDrainBeforeHangupTaskRuns() async throws {
        let client = try makeTakeoverClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
                                  onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {})
        let packet = media.requestHangup()
        media.finishCallKitAudioDrain()
        await waitUntil { transport.hangupPackets.count == 1 }
        XCTAssertFalse(transport.operations.contains(.disconnect))
        try await acknowledge(packet, on: transport)
        await media.stop()
        XCTAssertEqual(transport.hangupPackets, [packet])
        XCTAssertTrue(transport.operations.contains(.disconnect))
    }

    func testHangupKeepsControlUntilMatchingAcknowledgementAndEndsUIImmediately() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")

        let packet = media.requestHangup()
        XCTAssertEqual(states.last, .ended(label: "10086", reason: "local_hangup"))
        await waitUntil { transport.hangupPackets.count == 1 }
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: packet) as? [String: String])
        XCTAssertNotNil(UUID(uuidString: try XCTUnwrap(fields["commandId"])))
        await transport.emit(.data(Data(#"{"type":"hangup.ack","commandId":"wrong-command"}"#.utf8),
                                   topic: CallPilotTopic.status))
        await transport.emit(.data(Data(#"{"type":"status","status":"connected"}"#.utf8),
                                   topic: CallPilotTopic.status))
        XCTAssertFalse(transport.operations.contains(.disconnect))
        XCTAssertEqual(states.filter(\.isTerminal).count, 1)
        try await acknowledge(packet, on: transport)
        await waitUntil { transport.operations.contains(.disconnect) }
        await media.stop()
        XCTAssertEqual(transport.hangupPackets.count, 1)
        XCTAssertEqual(states.last, .ended(label: "10086", reason: "local_hangup"))
    }

    func testOldEdgeWithoutAcknowledgementDisconnectsAtBudgetWithoutUIError() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport },
                                    hangupBudget: .milliseconds(40))
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        let began = ContinuousClock.now
        _ = await media.hangup()
        XCTAssertGreaterThanOrEqual(began.duration(to: .now), .milliseconds(35))
        XCTAssertLessThan(began.duration(to: .now), .seconds(1))
        XCTAssertTrue(transport.operations.contains(.disconnect))
        XCTAssertEqual(transport.hangupPackets.count, 1)
        XCTAssertEqual(states.last, .ended(label: "10086", reason: "local_hangup"))
    }

    func testHangupRetriesFailedPublishOnceWithSameCommandID() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        transport.hangupFailuresRemaining = 1
        transport.acknowledgeHangup = true
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        let packet = await media.hangup()
        XCTAssertEqual(transport.hangupAttempts, [packet, packet])
        XCTAssertEqual(transport.hangupPackets, [packet])
        XCTAssertTrue(transport.operations.contains(.disconnect))
    }

    func testRepeatedLocalHangupReusesOneCommandAndOneTask() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        let first = media.requestHangup()
        let second = media.requestHangup()
        XCTAssertEqual(first, second)
        await waitUntil { transport.hangupPackets.count == 1 }
        try await acknowledge(first, on: transport)
        let third = await media.hangup()
        XCTAssertEqual(first, third)
        XCTAssertEqual(transport.hangupAttempts.count, 1)
    }

    func testHangupStopsRetryingAfterSecondFailureAndStillDisconnects() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        transport.hangupFailuresRemaining = 3
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport },
                                    hangupBudget: .milliseconds(40))
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        let packet = await media.hangup()
        XCTAssertEqual(transport.hangupAttempts, [packet, packet])
        XCTAssertTrue(transport.hangupPackets.isEmpty)
        XCTAssertTrue(transport.operations.contains(.disconnect))
        XCTAssertEqual(states.last, .ended(label: "10086", reason: "local_hangup"))
    }

    func testHangupBudgetBoundsAStuckPublish() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = PublishGate()
        transport.hangupPublishGate = gate
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport },
                                    hangupBudget: .milliseconds(40))
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        media.requestHangup()
        await waitUntil { gate.didStart }
        await waitUntil { transport.operations.contains(.disconnect) }
        await media.stop()
        XCTAssertTrue(transport.hangupPackets.isEmpty)
    }

    func testHangupDisconnectsBeforeJoiningSDKPublishThatIgnoresCancellation() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = ConnectGate()
        transport.hangupDisconnectGate = gate
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport },
                                    hangupBudget: .milliseconds(40))
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        media.requestHangup()
        await waitUntil { gate.didStart }
        await waitUntil { transport.operations.contains(.disconnect) }
        // Also release on failure so a regression reports an assertion instead of hanging XCTest.
        gate.open()
        await media.stop()
        XCTAssertEqual(transport.hangupAttempts.count, 1)
        XCTAssertTrue(transport.hangupPackets.isEmpty)
    }

    func testSynchronousHangupDuringConnectDoesNotReviveOrPublishDial() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = ConnectGate()
        transport.connectGate = gate
        transport.acknowledgeHangup = true
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport })
        let setup = Task { await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086") }
        await waitUntil { gate.didStart }
        media.requestHangup()
        XCTAssertTrue(states.last?.isTerminal == true)
        await waitUntil { transport.operations.contains(.disconnect) }
        gate.open()
        await setup.value
        await media.stop()
        XCTAssertEqual(transport.hangupPackets.count, 1)
        XCTAssertEqual(states.filter(\.isTerminal).count, 1)
        XCTAssertFalse(transport.operations.contains { operation in
            guard case let .publish(data) = operation,
                  let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String]
            else { return false }
            return fields["type"] == "dial"
        })
    }

    func testExpiredCallKitDrainStopsOldSessionWithoutStoppingReplacementOutbound() async throws {
        let client = try makeTakeoverClient()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let oldTransport = FakeCallMediaTransport()
        var oldStates: [CallState] = []
        let old = CallMediaSession(onState: { oldStates.append($0) },
                                   transportFactory: { oldTransport }, audio: audio)
        await old.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
                                onClaimed: { try old.prepareCallKitAnswer() }, onConnected: {})
        old.finishCallKitAudioDrain()
        XCTAssertGreaterThan(device.stopCount, 0)
        XCTAssertTrue(oldStates.last?.isTerminal == true)
        await old.stop()
        XCTAssertNil(audio.owner)
        XCTAssertTrue(oldTransport.operations.contains(.disconnect))

        let nextClient = try makeOutboundClient()
        let nextTransport = FakeCallMediaTransport()
        let next = CallMediaSession(onState: { _ in }, transportFactory: { nextTransport }, audio: audio)
        await next.startOutbound(client: nextClient, edgeId: "edge_abcdefghijkl", number: "10086")
        let stops = device.stopCount
        let owner = audio.owner
        old.finishCallKitAudioDrain()
        XCTAssertEqual(device.stopCount, stops)
        XCTAssertEqual(audio.owner, owner)
        XCTAssertEqual(oldStates.filter(\.isTerminal).count, 1)
        await next.stop()
    }

    func testStopCancelsInFlightDTMFBeforeReleasingAudio() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = PublishGate()
        transport.dtmfPublishGate = gate
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "1")
        let sending = Task { await media.sendDTMF("2") }
        await waitUntil { gate.didStart }
        await media.stop()
        await sending.value
        XCTAssertTrue(transport.controlPackets.isEmpty)
    }

    func testCancelledSetupEndsUIAndCannotBeRevivedByLateConnect() async throws {
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = ConnectGate()
        transport.connectGate = gate
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport })
        let setup = Task { await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "1") }
        await waitUntil { gate.didStart }
        setup.cancel()
        await waitUntil { transport.operations.contains(.disconnect) }
        XCTAssertTrue(states.last?.isTerminal == true)
        gate.open()
        await setup.value
        await media.stop()
        await transport.emit(.data(Data(#"{"type":"status","status":"connected"}"#.utf8),
                                   topic: CallPilotTopic.status))
        XCTAssertEqual(states.filter(\.isTerminal).count, 1)
    }

    func testCallKitClaimCompletesBeforeConnectAndActivationWaitsForRoom() async throws {
        let client = try makeTakeoverClient()
        let transport = FakeCallMediaTransport()
        let gate = ConnectGate()
        transport.connectGate = gate
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
        var claimed = false
        transport.onConnect = { XCTAssertTrue(claimed) }
        let setup = Task {
            await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
                                      onClaimed: {
                try media.prepareCallKitAnswer()
                claimed = true
                try media.activateCallKitAudio()
            }, onConnected: {})
        }
        await waitUntil { gate.didStart }
        XCTAssertEqual(device.configureCount, 1)
        XCTAssertEqual(transport.concurrentMicrophone, false)
        XCTAssertEqual(transport.microphoneEnableCount, 0)
        gate.open()
        _ = await setup.value
        await waitUntil { transport.microphoneEnableCount == 1 }
        try media.activateCallKitAudio()
        await Task.yield()
        XCTAssertEqual(transport.microphoneEnableCount, 1)
        await media.stop()
    }

    func testCallKitConnectedRoomWaitsForActivationNotEdgeConnected() async throws {
        let client = try makeTakeoverClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        let joined = await media.startTakeover(
            client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { try media.prepareCallKitAnswer() }, onConnected: {}
        )
        XCTAssertTrue(joined)
        XCTAssertEqual(transport.concurrentMicrophone, false)
        XCTAssertEqual(transport.microphoneEnableCount, 0)
        try media.activateCallKitAudio()
        await waitUntil { transport.microphoneEnableCount == 1 }
        // No Edge connected event was sent; it is not an opening condition.
        await media.stop()
        XCTAssertThrowsError(try media.activateCallKitAudio())
        XCTAssertEqual(transport.microphoneEnableCount, 1)
    }

    func testHTTPFailureCleansAudioWithoutCreatingTransport() async throws {
        MediaMockURLProtocol.requestHandler = { request in
            Self.httpResponse(for: request, statusCode: 503,
                              json: #"{"error":{"code":"EDGE_OFFLINE","message":"offline"}}"#)
        }
        defer { MediaMockURLProtocol.requestHandler = nil }
        let client = try makeClient()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        var created = 0
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: {
            created += 1
            return FakeCallMediaTransport()
        }, audio: audio)
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        XCTAssertEqual(created, 0)
        XCTAssertEqual(device.stopCount, 2)
        XCTAssertNil(audio.owner)
        XCTAssertTrue(states.last?.isTerminal == true)
    }

    func testClaimFailureNeverFulfillsOrConnectsAndCleansAudio() async throws {
        MediaMockURLProtocol.requestHandler = { _ in throw URLError(.notConnectedToInternet) }
        defer { MediaMockURLProtocol.requestHandler = nil }
        let client = try makeClient()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
        let joined = await media.startTakeover(
            client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
            onClaimed: { XCTFail("claim must not complete") }, onConnected: {}
        )
        XCTAssertFalse(joined)
        XCTAssertFalse(transport.operations.contains(.connect))
        XCTAssertEqual(device.stopCount, 2)
        XCTAssertNil(audio.owner)
    }

    func testStopCancelsMicrophonePublicationBeforeReleasingAudio() async throws {
        let client = try makeTakeoverClient()
        let transport = FakeCallMediaTransport()
        let gate = PublishGate()
        transport.microphoneGate = gate
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport }, audio: audio)
        await media.startTakeover(client: client, offerId: "offer_abcdefghijkl", usesCallKit: true,
                                  onClaimed: { try media.activateCallKitAudio() }, onConnected: {})
        await waitUntil { gate.didStart }
        await media.stop()
        XCTAssertNil(audio.owner)
        XCTAssertEqual(device.stopCount, 2)
        gate.open()
        XCTAssertEqual(transport.microphoneEnableCount, 1)
    }

    func testLateConnectIsReapedBeforeNextCallCanUseAudio() async throws {
        let client = try makeOutboundClient()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let oldTransport = FakeCallMediaTransport()
        let gate = ConnectGate()
        oldTransport.connectGate = gate
        let old = CallMediaSession(onState: { _ in }, transportFactory: { oldTransport }, audio: audio)
        let setup = Task { await old.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "1") }
        await waitUntil { gate.didStart }
        let stop = Task { await old.stop() }
        await waitUntil { oldTransport.operations.contains(.disconnect) }
        let newTransport = FakeCallMediaTransport()
        let next = CallMediaSession(onState: { _ in }, transportFactory: { newTransport }, audio: audio)
        let nextSetup = Task { await next.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "2") }
        await Task.yield()
        XCTAssertFalse(newTransport.operations.contains(.connect))
        gate.open()
        await setup.value
        await stop.value
        await nextSetup.value
        XCTAssertTrue(newTransport.operations.contains(.connect))
        XCTAssertEqual(newTransport.concurrentMicrophone, true)
        let stops = device.stopCount
        await old.stop()
        XCTAssertEqual(device.stopCount, stops)
        await next.stop()
    }

    func testStatusPayloadDrivesStateCallback() {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断`.
        var states: [CallState] = []
        let session = CallMediaSession(onState: { states.append($0) })

        XCTAssertEqual(
            session.handleEdgePayload(Data(#"{"type":"status","status":"dialing"}"#.utf8), label: "10086"),
            .state(.dialing(number: "10086"))
        )
        XCTAssertEqual(states, [.dialing(number: "10086")])
    }

    func testConnectedPayloadRunsHandlerBeforeStateCallback() async {
        // Android parity: CallManagerTest.`answerTakeover connect 期间 connected 到达不被覆盖回等待态`.
        var callbacks: [String] = []
        let session = CallMediaSession(onState: { _ in callbacks.append("state") })
        session.installConnectedHandler { callbacks.append("connected") }

        _ = session.handleEdgePayload(
            Data(#"{"type":"status","status":"connected"}"#.utf8),
            label: "来电接管"
        )

        XCTAssertEqual(callbacks, ["connected", "state"])
    }

    func testMalformedPayloadDoesNotMutateState() {
        // Android parity: SignalingTest.`未知类型与坏 JSON 返回 null 而不是崩溃`.
        var states: [CallState] = []
        let session = CallMediaSession(onState: { states.append($0) })

        XCTAssertNil(session.handleEdgePayload(Data("not json".utf8), label: "10086"))
        XCTAssertTrue(states.isEmpty)
    }

    func testControlMethodsBuildPacketsWithoutLiveKitDependency() async throws {
        // Android parity: SignalingTest.`dtmf 校验 0-9星井 1-16 位` and `hangup 命令`.
        let session = CallMediaSession(onState: { _ in })

        let dtmf = try CallSignaling.encodeDTMF("2")
        let dtmfFields = try XCTUnwrap(
            JSONSerialization.jsonObject(with: dtmf) as? [String: String]
        )
        let hangup = await session.hangup()
        let hangupFields = try XCTUnwrap(
            JSONSerialization.jsonObject(with: hangup) as? [String: String]
        )

        XCTAssertEqual(dtmfFields, ["type": "dtmf", "digits": "2"])
        XCTAssertEqual(hangupFields["type"], "hangup")
        XCTAssertNotNil(UUID(uuidString: try XCTUnwrap(hangupFields["commandId"])))
        XCTAssertEqual(Set(hangupFields.keys), ["type", "commandId"])
        XCTAssertThrowsError(try CallSignaling.encodeDTMF("invalid"))
    }

    func testHangupFencesLateStatusFromSameSession() async {
        // Android parity: CallManagerTest.`挂断与 media_ready 交错时绝不发送 dial`.
        var states: [CallState] = []
        let session = CallMediaSession(onState: { states.append($0) })

        _ = await session.hangup()
        XCTAssertNil(session.handleEdgePayload(
            Data(#"{"type":"status","status":"dialing"}"#.utf8),
            label: "10086"
        ))

        XCTAssertEqual(states, [.ended(label: "", reason: "local_hangup")])
        await session.sendDTMF("2")
        XCTAssertEqual(states, [.ended(label: "", reason: "local_hangup")])
    }

    func testStartOutboundPreparesSessionAndDefersDialPacket() async throws {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断` waits for media_ready.
        var requestCount = 0
        MediaMockURLProtocol.requestHandler = { request in
            requestCount += 1
            let json: String
            let statusCode: Int
            if request.httpMethod == "POST" {
                statusCode = 202
                json = #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"#
            } else {
                statusCode = 200
                json = #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}"#
            }
            return (
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: statusCode,
                    httpVersion: "HTTP/1.1",
                    headerFields: [:]
                )!,
                Data(json.utf8)
            )
        }
        defer { MediaMockURLProtocol.requestHandler = nil }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MediaMockURLProtocol.self]
        let client = try HostedCloudClient(
            baseURL: "https://cloud.example.test/",
            urlSession: URLSession(configuration: configuration),
            clockMilliseconds: { 1_000 },
            sleepMilliseconds: { _ in }
        )
        var states: [CallState] = []
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(
            onState: { states.append($0) },
            transportFactory: { transport }
        )

        await media.startOutbound(
            client: client,
            edgeId: "edge_abcdefghijkl",
            number: "10086"
        )

        XCTAssertEqual(requestCount, 2)
        XCTAssertEqual(media.preparedSession?.sessionId, "call_abcdefghijkl")
        let dialPacket = try XCTUnwrap(media.takePendingDialPacket())
        let dial = try XCTUnwrap(
            JSONSerialization.jsonObject(with: dialPacket) as? [String: String]
        )
        XCTAssertEqual(dial["type"], "dial")
        XCTAssertEqual(dial["number"], "10086")
        XCTAssertNil(media.takePendingDialPacket())
        XCTAssertEqual(states, [.waitingMedia(label: "10086")])
    }

    func testOutboundPublishesOneDialWhenMediaReadyArrivesDuringConnect() async throws {
        // Android parity: CallManagerTest.`media_ready 才发送 dial 且重复事件不重拨`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        transport.eventsDuringConnect = [
            .data(Data(#"{"type":"status","status":"media_ready"}"#.utf8), topic: CallPilotTopic.status)
        ]
        var states: [CallState] = []
        transport.onConnect = { XCTAssertEqual(states.last, .waitingMedia(label: "10086")) }
        let media = CallMediaSession(
            onState: { states.append($0) },
            transportFactory: { transport }
        )

        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        await transport.emit(
            .data(Data(#"{"type":"status","status":"media_ready"}"#.utf8), topic: CallPilotTopic.status)
        )

        let published = transport.controlPackets
        XCTAssertEqual(published.count, 1)
        let dial = try XCTUnwrap(JSONSerialization.jsonObject(with: published[0]) as? [String: String])
        XCTAssertEqual(dial["type"], "dial")
        XCTAssertEqual(dial["number"], "10086")
        XCTAssertEqual(transport.operations.first, .setSpeaker(false))
    }

    func testTakeoverConnectedDuringConnectIsNotOverwrittenAndNeverDials() async throws {
        // Android parity: CallManagerTest.`answerTakeover connect 期间 connected 到达不被覆盖回等待态`.
        let client = try makeTakeoverClient()
        let transport = FakeCallMediaTransport()
        transport.eventsDuringConnect = [
            .data(Data(#"{"type":"status","status":"connected"}"#.utf8), topic: CallPilotTopic.status)
        ]
        var states: [CallState] = []
        var connectedCount = 0
        let media = CallMediaSession(
            onState: { states.append($0) },
            transportFactory: { transport }
        )

        let joined = await media.startTakeover(
            client: client,
            offerId: "offer_abcdefghijkl",
            onConnected: { connectedCount += 1 }
        )

        XCTAssertTrue(joined)
        let label = L10n.text("call.takeover.label")
        XCTAssertEqual(states, [.waitingMedia(label: label), .inCall(label: label)])
        XCTAssertEqual(connectedCount, 1)
        XCTAssertTrue(transport.controlPackets.isEmpty)
    }

    func testDTMFPublishesOnlyValidatedControlPackets() async throws {
        // Android parity: SignalingTest.`dtmf 校验 0-9星井 1-16 位`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")

        await media.sendDTMF("2")
        await media.sendDTMF("invalid")

        let published = transport.controlPackets
        XCTAssertEqual(published.count, 1)
        let packet = try XCTUnwrap(JSONSerialization.jsonObject(with: published[0]) as? [String: String])
        XCTAssertEqual(packet, ["type": "dtmf", "digits": "2"])
    }

    func testHangupPublishesBeforeDisconnectAndFencesLateEvents() async throws {
        // Android parity: CallManagerTest.`完整生命周期 拨号到本地挂断`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")

        await media.hangup()
        await transport.emit(
            .data(Data(#"{"type":"status","status":"connected"}"#.utf8), topic: CallPilotTopic.status)
        )

        let publishIndex = try XCTUnwrap(transport.operations.firstIndex { operation in
            guard case let .publish(data) = operation,
                  let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String]
            else { return false }
            return fields["type"] == "hangup"
        })
        let disconnectIndex = try XCTUnwrap(transport.operations.firstIndex(of: .disconnect))
        XCTAssertLessThan(publishIndex, disconnectIndex)
        XCTAssertEqual(states.filter { state in
            if case .ended = state { return true }
            return false
        }.count, 1)
        XCTAssertEqual(states.last, .ended(label: "10086", reason: "local_hangup"))
    }

    func testHangupCancelsDialAlreadyWaitingToPublish() async throws {
        // Android parity: CallManagerTest.`挂断与 media_ready 交错时绝不发送 dial`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = PublishGate()
        transport.dialPublishGate = gate
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")

        let mediaReadyTask = Task {
            await transport.emit(
                .data(
                    Data(#"{"type":"status","status":"media_ready"}"#.utf8),
                    topic: CallPilotTopic.status
                )
            )
        }
        await waitUntil { gate.didStart }
        await media.hangup()
        gate.open()
        await mediaReadyTask.value

        let commandTypes = transport.operations.compactMap { operation -> String? in
            guard case let .publish(data) = operation,
                  let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String]
            else { return nil }
            return fields["type"]
        }
        XCTAssertEqual(commandTypes, ["hangup"])
    }

    func testUnexpectedDisconnectEndsActiveSessionButStopDoesNotDoubleFinish() async throws {
        // Android parity: CallManagerTest.`媒体断开时活动通话收尾`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")

        await transport.emit(.disconnected(reason: "network_lost"))
        await media.stop()
        await transport.emit(.disconnected(reason: "late_disconnect"))

        XCTAssertEqual(states.last, .ended(label: "10086", reason: "network_lost"))
        XCTAssertEqual(states.filter { state in
            if case .ended = state { return true }
            return false
        }.count, 1)
    }

    func testStopDuringConnectPreventsLateConnectCompletionFromRevivingSession() async throws {
        // Android parity: CallManagerTest.`hangup during setup does not revive call`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let gate = ConnectGate()
        transport.connectGate = gate
        var states: [CallState] = []
        let media = CallMediaSession(onState: { states.append($0) }, transportFactory: { transport })

        let startTask = Task {
            await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        }
        await waitUntil { gate.didStart }
        let stopTask = Task { await media.stop() }
        await waitUntil { transport.operations.contains(.disconnect) }
        gate.open()
        await stopTask.value
        await startTask.value
        await transport.emit(
            .data(Data(#"{"type":"status","status":"connected"}"#.utf8), topic: CallPilotTopic.status)
        )

        XCTAssertEqual(states, [.waitingMedia(label: "10086")])
        XCTAssertTrue(transport.operations.contains(.disconnect))
    }

    func testSpeakerDefaultsToReceiverAndToggleIsReversible() async throws {
        // Android parity: CallManagerTest.`speakerphone delegates to active session`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })

        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")
        media.setSpeakerphone(true)
        media.setSpeakerphone(false)

        XCTAssertEqual(
            transport.operations.filter { operation in
                if case .setSpeaker = operation { return true }
                return false
            },
            [.setSpeaker(false), .setSpeaker(true), .setSpeaker(false)]
        )
    }

    func testDelegateRelayPreservesBurstOrderWhileFirstDeliverySuspends() async {
        let firstDeliveryGate = PublishGate()
        let deliveredAll = expectation(description: "all delegate events delivered")
        let events: [CallMediaTransportEvent] = [
            .data(
                Data(#"{"type":"status","status":"media_ready"}"#.utf8),
                topic: CallPilotTopic.status
            ),
            .data(
                Data(#"{"type":"status","status":"dialing"}"#.utf8),
                topic: CallPilotTopic.status
            ),
            .data(
                Data(#"{"type":"status","status":"connected"}"#.utf8),
                topic: CallPilotTopic.status
            ),
            .data(
                Data(#"{"type":"status","status":"ended"}"#.utf8),
                topic: CallPilotTopic.status
            ),
        ]
        var actions: [CallStatusAction] = []
        var states: [CallState] = []
        var stateMachine = CallAttemptStateMachine()
        let attempt = stateMachine.begin(with: .waitingMedia(label: "10086"))
        let relay = CallMediaEventRelay { event in
            guard case let .data(data, topic) = event,
                  topic == CallPilotTopic.status,
                  let edgeEvent = CallSignaling.decodeEvent(data)
            else { return }
            let action = CallStatusReducer.reduce(edgeEvent, label: "10086")
            actions.append(action)
            if actions.count == 1 { try? await firstDeliveryGate.wait() }
            if case let .state(state) = action {
                XCTAssertTrue(stateMachine.transition(to: state, for: attempt))
                states.append(stateMachine.state)
            }
            if actions.count == events.count { deliveredAll.fulfill() }
        }

        for event in events { relay.yield(event) }
        await waitUntil { firstDeliveryGate.didStart }
        await Task.yield()
        XCTAssertEqual(actions, [.mediaReady])
        XCTAssertEqual(stateMachine.state, .waitingMedia(label: "10086"))

        firstDeliveryGate.open()
        await fulfillment(of: [deliveredAll], timeout: 1)
        XCTAssertEqual(actions, [
            .mediaReady,
            .state(.dialing(number: "10086")),
            .state(.inCall(label: "10086")),
            .state(.ended(label: "10086", reason: "ended")),
        ])
        XCTAssertEqual(states, [
            .dialing(number: "10086"),
            .inCall(label: "10086"),
            .ended(label: "10086", reason: "ended"),
        ])
    }

    private func makeOutboundClient() throws -> HostedCloudClient {
        MediaMockURLProtocol.requestHandler = { request in
            let json: String
            let statusCode: Int
            if request.httpMethod == "POST" {
                statusCode = 202
                json = #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"pending","createdAt":1,"expiresAt":9999}"#
            } else {
                statusCode = 200
                json = #"{"callId":"call_abcdefghijkl","edgeId":"edge_abcdefghijkl","status":"ready","createdAt":1,"expiresAt":9999,"session":{"livekitUrl":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}}"#
            }
            return Self.httpResponse(for: request, statusCode: statusCode, json: json)
        }
        return try makeClient()
    }

    private func makeTakeoverClient(offerId: String = "offer_abcdefghijkl") throws -> HostedCloudClient {
        MediaMockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/claim")
            return Self.httpResponse(
                for: request,
                statusCode: 202,
                json: #"{"claimId":"claim_abcdefghijkl","offerId":"\#(offerId)","edgeIdentity":"edgepart_abcdefghijkl","url":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}"#
            )
        }
        return try makeClient()
    }

    private func makeClient() throws -> HostedCloudClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MediaMockURLProtocol.self]
        return try HostedCloudClient(
            baseURL: "https://cloud.example.test/",
            urlSession: URLSession(configuration: configuration),
            clockMilliseconds: { 1_000 },
            sleepMilliseconds: { _ in }
        )
    }

    nonisolated private static func httpResponse(
        for request: URLRequest,
        statusCode: Int,
        json: String
    ) -> (HTTPURLResponse, Data) {
        (
            HTTPURLResponse(
                url: request.url!,
                statusCode: statusCode,
                httpVersion: "HTTP/1.1",
                headerFields: [:]
            )!,
            Data(json.utf8)
        )
    }

    private func acknowledge(_ packet: Data, on transport: FakeCallMediaTransport) async throws {
        let fields = try XCTUnwrap(JSONSerialization.jsonObject(with: packet) as? [String: String])
        let commandID = try XCTUnwrap(fields["commandId"])
        let acknowledgement = try JSONSerialization.data(withJSONObject: [
            "type": "hangup.ack", "commandId": commandID,
        ])
        await transport.emit(.data(acknowledgement, topic: CallPilotTopic.status))
    }

    private func waitUntil(
        timeout: Duration = .seconds(1),
        condition: @escaping @MainActor () -> Bool
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        while !condition(), clock.now < deadline {
            await Task.yield()
        }
        XCTAssertTrue(condition())
    }
}

@MainActor
private final class ManualGate {
    private var continuations: [CheckedContinuation<Void, Never>] = []
    private var isOpen = false
    private(set) var didStart = false

    func wait() async {
        didStart = true
        guard !isOpen else { return }
        await withCheckedContinuation { continuations.append($0) }
    }

    func open() {
        isOpen = true
        let pending = continuations
        continuations.removeAll()
        pending.forEach { $0.resume() }
    }
}

@MainActor
private final class ConnectGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private(set) var didStart = false

    func wait() async {
        didStart = true
        await withCheckedContinuation { continuation = $0 }
    }

    func open() {
        continuation?.resume()
        continuation = nil
    }
}

@MainActor
private final class PublishGate {
    private var continuation: CheckedContinuation<Void, Error>?
    private(set) var didStart = false

    func wait() async throws {
        didStart = true
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation = $0 }
        } onCancel: {
            Task { @MainActor [weak self] in self?.cancel() }
        }
    }

    func open() {
        continuation?.resume()
        continuation = nil
    }

    private func cancel() {
        continuation?.resume(throwing: CancellationError())
        continuation = nil
    }
}

private enum FakeTransportOperation: Equatable {
    case setSpeaker(Bool)
    case connect
    case publish(Data)
    case disconnect
}

@MainActor
private final class FakeCallMediaTransport: CallMediaTransport {
    var silentConnectCount = 0
    var silentConnectReturnCount = 0
    var silentConnectError: Error?
    var ignoreSilentCancellation = false
    var connectedURLs: [String] = []
    var connectedTokens: [String] = []
    var subscriptions: [String] = []
    var eventHandler: ((CallMediaTransportEvent) async -> Void)?
    var operations: [FakeTransportOperation] = []
    var eventsDuringConnect: [CallMediaTransportEvent] = []
    var onConnect: (() -> Void)?
    var connectGate: ConnectGate?
    var silentConnectGate: ManualGate?
    var disconnectGate: ManualGate?
    var thirdDisconnectGate: ManualGate?
    var disconnectCount = 0
    var dialPublishGate: PublishGate?
    var dtmfPublishGate: PublishGate?
    var concurrentMicrophone: Bool?
    var microphoneEnableCount = 0
    var microphoneGate: PublishGate?
    var hangupPublishGate: PublishGate?
    var hangupDisconnectGate: ConnectGate?
    var hangupFailuresRemaining = 0
    var hangupAttempts: [Data] = []
    var acknowledgeHangup = false

    var controlPackets: [Data] {
        operations.compactMap { operation in
            guard case let .publish(data) = operation,
                  let fields = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  fields["type"] as? String != "client_timing" else { return nil }
            return data
        }
    }

    var hangupPackets: [Data] {
        operations.compactMap { operation in
            guard case let .publish(data) = operation,
                  let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String],
                  fields["type"] == "hangup" else { return nil }
            return data
        }
    }

    func connect(url: String, token: String, enableMicrophone: Bool) async throws {
        connectedURLs.append(url)
        connectedTokens.append(token)
        concurrentMicrophone = enableMicrophone
        operations.append(.connect)
        onConnect?()
        if let connectGate { await connectGate.wait() }
        for event in eventsDuringConnect {
            await eventHandler?(event)
        }
    }

    func setMicrophone(enabled: Bool) async throws {
        microphoneEnableCount += 1
        if let microphoneGate { try await microphoneGate.wait() }
        try Task.checkCancellation()
    }

    func connectSilent(url: String, token: String) async throws {
        silentConnectCount += 1
        await silentConnectGate?.wait()
        if let connectGate { await connectGate.wait() }
        if !ignoreSilentCancellation { try Task.checkCancellation() }
        if let silentConnectError { throw silentConnectError }
        silentConnectReturnCount += 1
    }

    func subscribeToEdge(_ identity: String) async throws { subscriptions.append(identity) }

    func publishControl(_ data: Data) async throws {
        let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String]
        if fields?["type"] == "hangup" {
            hangupAttempts.append(data)
            if hangupFailuresRemaining > 0 {
                hangupFailuresRemaining -= 1
                throw URLError(.networkConnectionLost)
            }
            if let hangupPublishGate { try await hangupPublishGate.wait() }
            if let hangupDisconnectGate { await hangupDisconnectGate.wait() }
        }
        if let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String],
           fields["type"] == "dtmf", let dtmfPublishGate {
            try await dtmfPublishGate.wait()
        }
        if let fields = try? JSONSerialization.jsonObject(with: data) as? [String: String],
           fields["type"] == "dial",
           let dialPublishGate {
            try await dialPublishGate.wait()
        }
        try Task.checkCancellation()
        operations.append(.publish(data))
        if acknowledgeHangup, fields?["type"] == "hangup", let commandID = fields?["commandId"] {
            let acknowledgement = try JSONSerialization.data(withJSONObject: [
                "type": "hangup.ack", "commandId": commandID,
            ])
            await eventHandler?(.data(acknowledgement, topic: CallPilotTopic.status))
        }
    }

    func disconnect() async {
        disconnectCount += 1
        if disconnectCount == 3 { await thirdDisconnectGate?.wait() }
        await disconnectGate?.wait()
        operations.append(.disconnect)
        connectGate?.open()
        hangupDisconnectGate?.open()
    }

    func setSpeakerphone(_ enabled: Bool) {
        operations.append(.setSpeaker(enabled))
    }

    func emit(_ event: CallMediaTransportEvent) async {
        await eventHandler?(event)
    }
}

private final class ResponseGate: @unchecked Sendable {
    private let lock = NSLock()
    private let semaphore = DispatchSemaphore(value: 0)
    private var started = false

    var didStart: Bool { lock.withLock { started } }

    func wait() {
        lock.withLock { started = true }
        // Bound a failed test without requiring URLSession to cooperate with cancellation.
        _ = semaphore.wait(timeout: .now() + 10)
    }

    func open() { semaphore.signal() }
}

@MainActor
private extension CallMediaSession {
    convenience init(
        onState: @escaping StateHandler,
        transportFactory: @escaping TransportFactory = { FakeCallMediaTransport() },
        hangupBudget: Duration = .milliseconds(100)
    ) {
        self.init(onState: onState, transportFactory: transportFactory,
                  audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }),
                  hangupBudget: hangupBudget)
    }
}

private final class MediaMockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requestHandler: (
        (URLRequest) throws -> (HTTPURLResponse, Data)
    )?
    private let lock = NSLock()
    private var stopped = false

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        // A gated prepare must not block URLProtocol from starting the cold claim.
        DispatchQueue.global().async(execute: DispatchWorkItem { [self] in
            do {
                let (response, data) = try handler(request)
                guard !lock.withLock({ stopped }) else { return }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                guard !lock.withLock({ stopped }) else { return }
                client?.urlProtocol(self, didFailWithError: error)
            }
        })
    }

    override func stopLoading() { lock.withLock { stopped = true } }
}
