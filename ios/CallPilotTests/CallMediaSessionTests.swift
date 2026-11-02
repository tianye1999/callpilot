import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallMediaSessionTests: XCTestCase {
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

    func testAnswerWhilePrepareWaitsThenReusesSilentConnection() async throws {
        try await assertWaitingTakeoverReuses(at: .prepare)
    }

    func testAnswerWhileAudioLeaseWaitsThenReusesSilentConnection() async throws {
        try await assertWaitingTakeoverReuses(at: .audioLease)
    }

    func testAnswerWhileSilentConnectWaitsThenReusesOneConnection() async throws {
        try await assertWaitingTakeoverReuses(at: .connect)
    }

    func testAnswerWhilePrepareExceedsWaitBudgetFallsBackToCold() async throws {
        try await assertColdTakeover(at: .prepare)
    }

    func testAnswerWaitsThreeSecondsBeforeColdFallbackAndFencesLateEvents() async throws {
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

    private func assertWaitingTakeoverReuses(at stage: SilentAnswerStage) async throws {
        let responseGate = stage == .prepare ? ResponseGate() : nil
        let client = try makePreparedClient(mode: "reuse", prepareGate: responseGate)
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        let drain = ConnectGate()
        var cleanup: Task<Void, Never>?
        if stage == .audioLease {
            let previous = UUID()
            try await audio.acquire(previous, mode: .callKit)
            cleanup = audio.release(previous) { await drain.wait() }
            await waitUntil { drain.didStart }
        }
        let transport = FakeCallMediaTransport()
        if stage == .connect { transport.connectGate = ConnectGate() }
        var factoryCount = 0
        var claimed = false
        let media = CallMediaSession(onState: { _ in }, transportFactory: {
            factoryCount += 1
            return transport
        }, audio: audio)
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        switch stage {
        case .audioLease: await waitUntil { self.timingMarks(media)[.prepareResponse] != nil }
        case .prepare: await waitUntil { responseGate?.didStart == true }
        case .connect: await waitUntil { transport.connectGate?.didStart == true }
        case .ready: await preparing.value
        }
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true, onClaimed: {
                claimed = true
                try media.prepareCallKitAnswer()
            }, onConnected: {}) }
        await waitUntil { self.field(media, "claimStarted", as: Bool.self) == true }
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertFalse(claimed)
        XCTAssertNil(timingMarks(media)[.claimStart])
        XCTAssertNil(timingMarks(media)[.coldFallback])
        XCTAssertEqual(device.configureCount, 0)
        XCTAssertEqual(device.activateCount, 0)
        responseGate?.open()
        drain.open()
        transport.connectGate?.open()
        let joined = await answering.value
        await preparing.value
        await cleanup?.value
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
            var factoryCount = 0
            let media = CallMediaSession(onState: { _ in }, transportFactory: {
                factoryCount += 1
                if factoryCount == 2 { XCTAssertTrue(silent.operations.contains(.disconnect)) }
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

    func testPrepareFailureDuringAnswerWaitFallsBackImmediately() async throws {
        let gate = ResponseGate()
        let client = try makePreparedClient(mode: "cold", prepareGate: gate, failPrepare: true)
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        let preparing = Task { await media.startSilentPreconnect(client: client,
            offer: InboundOffer(offerId: "offer_abcdefghijkl", expiresAt: 9999)) }
        await waitUntil { gate.didStart }
        let answering = Task { await media.startTakeover(client: client,
            offerId: "offer_abcdefghijkl", usesCallKit: true, onConnected: {}) }
        await waitUntil { self.field(media, "claimStarted", as: Bool.self) == true }
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertNil(timingMarks(media)[.claimStart])
        let failedAt = ContinuousClock.now
        gate.open()
        await waitUntil { self.timingMarks(media)[.claimStart] != nil }
        let joined = await answering.value
        await preparing.value
        XCTAssertTrue(joined)
        XCTAssertLessThan(failedAt.duration(to: .now), .seconds(1))
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
        await waitUntil(timeout: .seconds(4)) { self.timingMarks(media)[.coldFallback] != nil }
        if stage != .ready {
            XCTAssertGreaterThanOrEqual(answeredAt.duration(to: .now), .seconds(3))
            XCTAssertLessThan(answeredAt.duration(to: .now), .seconds(4))
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

    func testColdFallbackClosesPreparedTransportBeforeConnectingAndKeepsLease() async throws {
        let client = try makePreparedClient(mode: "cold")
        let silent = FakeCallMediaTransport(), cold = FakeCallMediaTransport()
        let device = TestCallAudioDevice()
        let audio = CallAudioOwner(device: device, onCleanupError: { _ in XCTFail("cleanup") })
        var count = 0
        let media = CallMediaSession(onState: { _ in }, transportFactory: {
            count += 1
            if count == 2 { XCTAssertTrue(silent.operations.contains(.disconnect)) }
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

    func testAnswerDuringSilentConnectCancelsAndDrainsBeforeSingleColdClaim() async throws {
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
                                    failPrepare: Bool = false) throws -> HostedCloudClient {
        MediaMockURLProtocol.requestHandler = { request in
            if request.url?.path == "/v1/inbound-offers/prepare" {
                prepareGate?.wait()
                if failPrepare { throw URLError(.cannotConnectToHost) }
                return Self.httpResponse(for: request, statusCode: 200,
                    json: #"{"prepareId":"prepare_abcdefghijkl","roomName":"callpilot_abcdefghijkl","participantIdentity":"web_abcdefghijkl","url":"wss://lk.example.com","token":"restricted-token","expiresAt":9999}"#)
            }
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/claim")
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
                json: #"{"claimId":"claim_abcdefghijkl","offerId":"\#(offerId)","url":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}"#
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
        if let connectGate { await connectGate.wait() }
        if !ignoreSilentCancellation { try Task.checkCancellation() }
        if let silentConnectError { throw silentConnectError }
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

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
