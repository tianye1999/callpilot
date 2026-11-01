import Foundation
import XCTest
@testable import CallPilot

@MainActor
final class CallMediaSessionTests: XCTestCase {
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
        XCTAssertFalse(transport.operations.contains { if case .publish = $0 { return true }; return false })
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
        XCTAssertEqual(hangupFields, ["type": "hangup"])
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

        let published = transport.operations.compactMap { operation -> Data? in
            guard case let .publish(data) = operation else { return nil }
            return data
        }
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
        XCTAssertFalse(transport.operations.contains { operation in
            if case .publish = operation { return true }
            return false
        })
    }

    func testDTMFPublishesOnlyValidatedControlPackets() async throws {
        // Android parity: SignalingTest.`dtmf 校验 0-9星井 1-16 位`.
        let client = try makeOutboundClient()
        let transport = FakeCallMediaTransport()
        let media = CallMediaSession(onState: { _ in }, transportFactory: { transport })
        await media.startOutbound(client: client, edgeId: "edge_abcdefghijkl", number: "10086")

        await media.sendDTMF("2")
        await media.sendDTMF("invalid")

        let published = transport.operations.compactMap { operation -> Data? in
            guard case let .publish(data) = operation else { return nil }
            return data
        }
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

    private func makeTakeoverClient() throws -> HostedCloudClient {
        MediaMockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/v1/inbound-offers/claim")
            return Self.httpResponse(
                for: request,
                statusCode: 202,
                json: #"{"claimId":"claim_abcdefghijkl","offerId":"offer_abcdefghijkl","url":"wss://lk.example.com","token":"jwt-token","expiresAt":9999}"#
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

    func connect(url: String, token: String, enableMicrophone: Bool) async throws {
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

    func publishControl(_ data: Data) async throws {
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
    }

    func disconnect() async {
        operations.append(.disconnect)
    }

    func setSpeakerphone(_ enabled: Bool) {
        operations.append(.setSpeaker(enabled))
    }

    func emit(_ event: CallMediaTransportEvent) async {
        await eventHandler?(event)
    }
}

@MainActor
private extension CallMediaSession {
    convenience init(
        onState: @escaping StateHandler,
        transportFactory: @escaping TransportFactory = { FakeCallMediaTransport() }
    ) {
        self.init(onState: onState, transportFactory: transportFactory,
                  audio: CallAudioOwner(device: TestCallAudioDevice(), onCleanupError: { _ in }))
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
