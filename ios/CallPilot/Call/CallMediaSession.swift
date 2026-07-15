import Foundation

/// Owns one LiveKit media attempt and translates Edge data-topic events into
/// the platform-neutral call state machine.
@MainActor
final class CallMediaSession {
    typealias TransportFactory = @MainActor () -> any CallMediaTransport
    typealias StateHandler = @MainActor (CallState) -> Void

    private let onState: StateHandler
    private let transportFactory: TransportFactory
    private var connectedHandler: (() -> Void)?
    private var transport: (any CallMediaTransport)?
    private(set) var preparedSession: HostedCallSession?
    private(set) var pendingDialPacket: Data?
    private var activeLabel: String?
    private var isStopped = false
    private var generation: UInt64 = 0
    private var speakerphoneEnabled = false
    private var dialTask: Task<Void, Never>?

    init(
        onState: @escaping StateHandler,
        transportFactory: @escaping TransportFactory = { LiveKitRoomTransport() }
    ) {
        self.onState = onState
        self.transportFactory = transportFactory
    }

    /// US-2 outbound: create a hosted room, publish the microphone while
    /// connecting, then send dial only after Edge reports media_ready.
    func startOutbound(client: HostedCloudClient, edgeId: String, number: String) async {
        let attempt = beginAttempt(label: number)
        do {
            let dialPacket = try CallSignaling.encodeDial(
                number: number,
                idempotencyKey: UUID().uuidString
            )
            let session = try await client.createSession(edgeId: edgeId)
            guard isCurrent(attempt) else { return }

            preparedSession = session
            pendingDialPacket = dialPacket
            let activeTransport = installTransport(for: attempt)
            activeTransport.setSpeakerphone(speakerphoneEnabled)

            // Edge can emit media_ready from inside connect, so WaitingMedia
            // must be visible before the SDK begins connecting.
            onState(.waitingMedia(label: number))
            try await activeTransport.connect(url: session.livekitURL, token: session.token)
            guard isCurrent(attempt) else {
                await activeTransport.disconnect()
                return
            }
        } catch let error as HostedCloudError {
            await failSetup(
                attempt: attempt,
                label: number,
                reason: error.message,
                code: error.code
            )
        } catch {
            await failSetup(
                attempt: attempt,
                label: number,
                reason: error.localizedDescription,
                code: nil
            )
        }
    }

    /// US-1 takeover: claim the offer and join its room. The physical call is
    /// already active, so this path never sends dial.
    func startTakeover(
        client: HostedCloudClient,
        offerId: String,
        onConnected: @escaping () -> Void
    ) async {
        let label = "来电接管"
        let attempt = beginAttempt(label: label)
        installConnectedHandler(onConnected)
        do {
            let session = try await client.claimInboundOffer(offerId: offerId)
            guard isCurrent(attempt) else { return }

            preparedSession = session
            let activeTransport = installTransport(for: attempt)
            activeTransport.setSpeakerphone(speakerphoneEnabled)

            // connected can arrive during Room.connect. Publishing waiting
            // first prevents a late write from reverting InCall.
            onState(.waitingMedia(label: label))
            try await activeTransport.connect(url: session.livekitURL, token: session.token)
            guard isCurrent(attempt) else {
                await activeTransport.disconnect()
                return
            }
        } catch let error as HostedCloudError {
            await failSetup(
                attempt: attempt,
                label: label,
                reason: error.message,
                code: error.code
            )
        } catch {
            await failSetup(
                attempt: attempt,
                label: label,
                reason: error.localizedDescription,
                code: nil
            )
        }
    }

    /// Edge status=connected arrives before this is called. Running the
    /// timeout cancellation callback first preserves AppModel's attempt fence.
    func handleConnected(label: String) {
        guard !isStopped else { return }
        connectedHandler?()
        connectedHandler = nil
        onState(.inCall(label: label))
    }

    @discardableResult
    func handleEdgePayload(_ data: Data, label: String) -> CallStatusAction? {
        guard !isStopped else { return nil }
        activeLabel = label
        guard let event = CallSignaling.decodeEvent(data) else { return nil }
        let action = CallStatusReducer.reduce(event, label: label)
        switch action {
        case .mediaReady, .ignored:
            break
        case let .state(state):
            if case .inCall = state {
                handleConnected(label: label)
            } else {
                if !state.isActive { _ = fenceCurrentAttempt() }
                onState(state)
            }
        }
        return action
    }

    /// Atomically consumes the deferred dial command. Repeated media_ready
    /// events therefore cannot redial the same physical line.
    func takePendingDialPacket() -> Data? {
        defer { pendingDialPacket = nil }
        return pendingDialPacket
    }

    func installConnectedHandler(_ handler: @escaping () -> Void) {
        connectedHandler = handler
    }

    func sendDTMF(_ digit: String) async {
        guard !isStopped,
              let activeTransport = transport,
              let packet = try? CallSignaling.encodeDTMF(digit)
        else { return }
        // A failed DTMF packet does not end an otherwise healthy call.
        try? await activeTransport.publishControl(packet)
    }

    @discardableResult
    func hangup() async -> Data {
        let packet = CallSignaling.encodeHangup()
        guard !isStopped else { return packet }
        let label = activeLabel ?? "通话"
        let activeTransport = fenceCurrentAttempt()

        // Fence first, then deliver the reliable command before disconnecting.
        if let activeTransport {
            try? await activeTransport.publishControl(packet)
            await activeTransport.disconnect()
        }
        activeLabel = nil
        onState(.ended(label: label, reason: "local_hangup"))
        return packet
    }

    func stop() async {
        let activeTransport = fenceCurrentAttempt()
        activeLabel = nil
        await activeTransport?.disconnect()
    }

    func setSpeakerphone(_ enabled: Bool) {
        guard !isStopped else { return }
        speakerphoneEnabled = enabled
        transport?.setSpeakerphone(enabled)
    }

    private func beginAttempt(label: String) -> UInt64 {
        generation &+= 1
        dialTask?.cancel()
        dialTask = nil
        isStopped = false
        activeLabel = label
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        transport = nil
        speakerphoneEnabled = false
        return generation
    }

    private func installTransport(for attempt: UInt64) -> any CallMediaTransport {
        let activeTransport = transportFactory()
        activeTransport.eventHandler = { [weak self] event in
            guard let self else { return }
            await self.handleTransportEvent(event, for: attempt)
        }
        transport = activeTransport
        return activeTransport
    }

    private func handleTransportEvent(
        _ event: CallMediaTransportEvent,
        for attempt: UInt64
    ) async {
        guard isCurrent(attempt) else { return }
        switch event {
        case let .data(data, topic):
            guard topic == CallPilotTopic.status else { return }
            let activeTransport = transport
            let action = handleEdgePayload(data, label: activeLabel ?? "通话")
            switch action {
            case .mediaReady:
                guard isCurrent(attempt),
                      let activeTransport,
                      let packet = takePendingDialPacket()
                else { return }
                await publishPendingDial(
                    packet,
                    using: activeTransport,
                    for: attempt
                )
            case let .state(state) where !state.isActive:
                activeTransport?.eventHandler = nil
                await activeTransport?.disconnect()
            case .state, .ignored, nil:
                break
            }
        case let .disconnected(reason):
            guard isCurrent(attempt) else { return }
            let label = activeLabel ?? "通话"
            _ = fenceCurrentAttempt()
            activeLabel = nil
            onState(.ended(label: label, reason: reason))
        }
    }

    private func failSetup(
        attempt: UInt64,
        label: String,
        reason: String,
        code: String?
    ) async {
        guard isCurrent(attempt) else { return }
        let activeTransport = fenceCurrentAttempt()
        await activeTransport?.disconnect()
        activeLabel = nil
        onState(.failed(label: label, reason: reason, code: code))
    }

    private func publishPendingDial(
        _ packet: Data,
        using activeTransport: any CallMediaTransport,
        for attempt: UInt64
    ) async {
        let task = Task { @MainActor [weak self] in
            guard let self, isCurrent(attempt), !Task.isCancelled else { return }
            do {
                try Task.checkCancellation()
                try await activeTransport.publishControl(packet)
            } catch is CancellationError {
                return
            } catch {
                await failActive(
                    attempt: attempt,
                    reason: "发送拨号命令失败",
                    code: "MEDIA_COMMAND_FAILED"
                )
            }
        }
        dialTask = task
        await task.value
        if isCurrent(attempt) { dialTask = nil }
    }

    private func failActive(attempt: UInt64, reason: String, code: String?) async {
        guard isCurrent(attempt) else { return }
        let label = activeLabel ?? "通话"
        let activeTransport = fenceCurrentAttempt()
        await activeTransport?.disconnect()
        activeLabel = nil
        onState(.failed(label: label, reason: reason, code: code))
    }

    private func isCurrent(_ attempt: UInt64) -> Bool {
        !isStopped && generation == attempt
    }

    @discardableResult
    private func fenceCurrentAttempt() -> (any CallMediaTransport)? {
        guard !isStopped else { return nil }
        isStopped = true
        generation &+= 1
        dialTask?.cancel()
        dialTask = nil
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        let activeTransport = transport
        activeTransport?.eventHandler = nil
        transport = nil
        return activeTransport
    }
}
