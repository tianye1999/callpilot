import Foundation

/// Owns one call attempt, including setup, microphone publication and teardown.
@MainActor
final class CallMediaSession {
    typealias TransportFactory = @MainActor () -> any CallMediaTransport
    typealias StateHandler = @MainActor (CallState) -> Void

    private let onState: StateHandler
    private let transportFactory: TransportFactory
    private let audio: CallAudioOwner
    private let ownerID = UUID()
    private var connectedHandler: (() -> Void)?
    private var transport: (any CallMediaTransport)?
    private(set) var preparedSession: HostedCallSession?
    private(set) var pendingDialPacket: Data?
    private var activeLabel: String?
    private var isStopped = false
    private var started = false
    private var generation: UInt64 = 0
    private var speakerphoneEnabled = false
    private var mode: CallAudioMode = .standalone
    private var roomConnected = false
    private var audioActivated = false
    private var microphoneStarted = false
    private var setupTask: Task<Void, Error>?
    private var microphoneTask: Task<Void, Error>?
    private var controlTasks: [UUID: Task<Void, Error>] = [:]
    private var cleanupTask: Task<Void, Never>?
    static let hangupBudget: Duration = .milliseconds(2500)
    private let hangupBudget: Duration
    let timing: CallTiming
    private var timingTask: Task<Void, Never>?
    private var supportsClientTiming = false
    private var hangupPacket: Data?
    private var hangupCommandID: String?
    private var hangupAcknowledged = false
    private var hangupDeadline: ContinuousClock.Instant?

    init(onState: @escaping StateHandler,
         transportFactory: @escaping TransportFactory,
         audio: CallAudioOwner,
         hangupBudget: Duration = CallMediaSession.hangupBudget,
         timing: CallTiming = CallTiming()) {
        self.onState = onState
        self.transportFactory = transportFactory
        self.audio = audio
        self.hangupBudget = hangupBudget
        self.timing = timing
    }

    func startOutbound(client: HostedCloudClient, edgeId: String, number: String) async {
        guard let attempt = beginAttempt(label: number, mode: .standalone) else { return }
        timing.mark(.dialStart)
        let task = Task {
            try await audio.acquire(ownerID, mode: .standalone)
            try checkCurrent(attempt)
            let packet = try CallSignaling.encodeDial(number: number, idempotencyKey: UUID().uuidString)
            timing.mark(.claimStart)
            let session = try await client.createSession(edgeId: edgeId)
            timing.mark(.claimResponse)
            try checkCurrent(attempt)
            preparedSession = session
            pendingDialPacket = packet
            try await connect(session, for: attempt)
        }
        setupTask = task
        await finishSetup(task, attempt: attempt)
    }

    /// Edge waits for our microphone subscription before committing phone-first takeover.
    @discardableResult
    func startTakeover(
        client: HostedCloudClient,
        offerId: String,
        usesCallKit: Bool = false,
        onClaimed: @escaping () throws -> Void = {},
        onConnected: @escaping () -> Void
    ) async -> Bool {
        guard let attempt = beginAttempt(
            label: L10n.text("call.takeover.label"), mode: usesCallKit ? .callKit : .standalone
        ) else { return false }
        connectedHandler = onConnected
        let task = Task {
            try await audio.acquire(ownerID, mode: mode)
            try checkCurrent(attempt)
            timing.startClaim()
            let session = try await client.claimInboundOffer(offerId: offerId, timing: timing)
            timing.mark(.claimResponse)
            try checkCurrent(attempt)
            preparedSession = session
            // Configure + fulfill before starting any media connection.
            try onClaimed()
            try checkCurrent(attempt)
            try await connect(session, for: attempt)
        }
        setupTask = task
        await finishSetup(task, attempt: attempt)
        return isCurrent(attempt)
    }

    func prepareCallKitAnswer() throws {
        guard !isStopped, mode == .callKit else { throw CancellationError() }
        timing.mark(.configureStart)
        try audio.configureCallKitSession(ownerID)
        timing.mark(.configureEnd)
    }

    func activateCallKitAudio() throws {
        timing.mark(.didActivate)
        guard !isStopped, mode == .callKit else { throw CancellationError() }
        guard !audioActivated else { return }
        try audio.activate(ownerID)
        timing.mark(.activateReturn)
        audioActivated = true
        timing.mark(.audioActivated)
        scheduleTiming()
        startMicrophoneIfReady(for: generation)
    }

    private func connect(_ session: HostedCallSession, for attempt: UInt64) async throws {
        let activeTransport = transportFactory()
        transport = activeTransport
        activeTransport.eventHandler = { [weak self] event in
            await self?.handleTransportEvent(event, for: attempt)
        }
        activeTransport.setSpeakerphone(speakerphoneEnabled)
        onState(.waitingMedia(label: activeLabel ?? ""))
        timing.mark(.connectStart)
        if mode == .standalone { timing.mark(.micEnableStart) }
        // Standalone calls retain the tested low-latency concurrent mic path.
        try await activeTransport.connect(
            url: session.livekitURL, token: session.token, enableMicrophone: mode == .standalone
        )
        timing.mark(.connectReturn)
        try checkCurrent(attempt)
        roomConnected = true
        timing.mark(.roomConnected)
        if mode == .standalone { timing.mark(.micEnableReturn) }
        scheduleTiming()
        startMicrophoneIfReady(for: attempt)
    }

    private func startMicrophoneIfReady(for attempt: UInt64) {
        guard isCurrent(attempt), mode == .callKit, roomConnected, audioActivated,
              !microphoneStarted, let activeTransport = transport else { return }
        microphoneStarted = true
        let task = Task {
            try checkCurrent(attempt)
            timing.mark(.micEnableStart)
            try await activeTransport.setMicrophone(enabled: true)
            timing.mark(.micEnableReturn)
            scheduleTiming()
            try checkCurrent(attempt)
        }
        microphoneTask = task
        Task { [weak self] in
            let result = await task.result
            guard let self, isCurrent(attempt) else { return }
            if case .failure = result {
                fail(reason: L10n.text("call.audio.failed"), code: "MICROPHONE_PUBLISH_FAILED")
            }
        }
    }

    private func finishSetup(_ task: Task<Void, Error>, attempt: UInt64) async {
        await withTaskCancellationHandler {
            let result = await task.result
            guard isCurrent(attempt) else { return }
            if case let .failure(error) = result {
                if let error = error as? HostedCloudError {
                    fail(reason: error.message, code: error.code)
                } else {
                    fail(reason: L10n.text("call.audio.failed"), code: "MEDIA_SETUP_FAILED")
                }
                await cleanupTask?.value
            }
        } onCancel: {
            task.cancel()
            Task { @MainActor [weak self] in
                guard let self else { return }
                terminate(state: .ended(label: activeLabel ?? "", reason: "setup_cancelled"))
            }
        }
    }

    func handleConnected(label: String) {
        guard !isStopped else { return }
        timing.mark(.edgeConnected)
        scheduleTiming()
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
            } else if state.isTerminal {
                terminate(state: state)
            } else {
                onState(state)
            }
        }
        return action
    }

    func takePendingDialPacket() -> Data? {
        defer { pendingDialPacket = nil }
        return pendingDialPacket
    }

    func installConnectedHandler(_ handler: @escaping () -> Void) { connectedHandler = handler }

    func sendDTMF(_ digit: String) async {
        guard !isStopped, let activeTransport = transport,
              let packet = try? CallSignaling.encodeDTMF(digit) else { return }
        try? await publishControl(packet, using: activeTransport, for: generation)
    }

    @discardableResult
    func hangup() async -> Data {
        let packet = requestHangup()
        await cleanupTask?.value
        return packet
    }

    @discardableResult
    func requestHangup() -> Data {
        if let hangupPacket { return hangupPacket }
        let id = UUID().uuidString
        let packet = CallSignaling.encodeHangup(commandId: id)
        hangupCommandID = id
        hangupPacket = packet
        hangupDeadline = ContinuousClock.now.advanced(by: hangupBudget)
        timing.mark(.hangupIntent)
        terminate(state: .ended(label: activeLabel ?? "", reason: "local_hangup"), hangup: packet)
        return packet
    }

    func stop() async {
        terminate(state: nil)
        await cleanupTask?.value
    }

    func finishCallKitAudioDrain() {
        terminate(state: .ended(label: activeLabel ?? "", reason: "callkit_ended"))
        // Native cleanup may still be settling. Stop only this lease; acquisition
        // remains serialized until its disconnect/publish tasks have returned.
        audio.stopIfOwned(ownerID)
    }

    func fail(reason: String, code: String) {
        terminate(state: .failed(label: activeLabel ?? "", reason: reason, code: code))
    }

    func setSpeakerphone(_ enabled: Bool) {
        guard !isStopped else { return }
        speakerphoneEnabled = enabled
        transport?.setSpeakerphone(enabled)
    }

    private func beginAttempt(label: String, mode: CallAudioMode) -> UInt64? {
        guard !started, !isStopped else { return nil }
        started = true
        generation &+= 1
        activeLabel = label
        self.mode = mode
        return generation
    }

    private func handleTransportEvent(_ event: CallMediaTransportEvent, for attempt: UInt64) async {
        if case let .data(data, topic) = event, topic == CallPilotTopic.status,
           let id = CallSignaling.decodeHangupAck(data), id == hangupCommandID {
            hangupAcknowledged = true
            timing.mark(.hangupAck)
            return
        }
        guard isCurrent(attempt) else { return }
        switch event {
        case let .data(data, topic):
            guard topic == CallPilotTopic.status else { return }
            if CallSignaling.supportsClientTiming(data) {
                supportsClientTiming = true
                scheduleTiming()
            }
            let action = handleEdgePayload(data, label: activeLabel ?? "")
            if case .mediaReady = action,
               isCurrent(attempt), let activeTransport = transport,
               let packet = takePendingDialPacket() {
                do {
                    try await publishControl(packet, using: activeTransport, for: attempt)
                } catch {
                    guard isCurrent(attempt) else { return }
                    fail(reason: L10n.text("call.audio.failed"), code: "MEDIA_COMMAND_FAILED")
                }
            }
        case let .disconnected(reason):
            terminate(state: .ended(label: activeLabel ?? "", reason: reason))
        case let .timing(mark, instant):
            timing.mark(mark, at: instant)
            scheduleTiming()
        }
    }

    private func scheduleTiming() {
        guard !isStopped, roomConnected, supportsClientTiming, timingTask == nil,
              let transport else { return }
        timingTask = Task { [weak self] in
            defer { self?.timingTask = nil }
            do { try await Task.sleep(for: .milliseconds(30)) } catch { return }
            guard let self else { return }
            while !isStopped, !Task.isCancelled, let packet = timing.nextBatch() {
                do { try await transport.publishControl(packet) } catch { return }
            }
        }
    }

    private func isCurrent(_ attempt: UInt64) -> Bool {
        !isStopped && generation == attempt
    }

    private func publishControl(
        _ packet: Data, using transport: any CallMediaTransport, for attempt: UInt64
    ) async throws {
        let id = UUID()
        let task = Task {
            try checkCurrent(attempt)
            try await transport.publishControl(packet)
        }
        controlTasks[id] = task
        defer { controlTasks.removeValue(forKey: id) }
        try await task.value
    }

    private func checkCurrent(_ attempt: UInt64) throws {
        try Task.checkCancellation()
        guard isCurrent(attempt) else { throw CancellationError() }
    }

    private func terminate(state: CallState?, hangup: Data? = nil) {
        guard !isStopped else { return }
        isStopped = true
        generation &+= 1
        let setup = setupTask
        let microphone = microphoneTask
        let controls = Array(controlTasks.values)
        let telemetry = timingTask
        telemetry?.cancel()
        timingTask = nil
        setup?.cancel()
        microphone?.cancel()
        controls.forEach { $0.cancel() }
        setupTask = nil
        microphoneTask = nil
        controlTasks.removeAll()
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        let activeTransport = transport
        if hangup == nil { activeTransport?.eventHandler = nil }
        transport = nil
        cleanupTask = audio.release(ownerID) {
            if let hangup, let activeTransport {
                await self.sendHangup(hangup, using: activeTransport)
            }
            self.timing.mark(.disconnect)
            activeTransport?.eventHandler = nil
            await activeTransport?.disconnect()
            // Disconnect unblocks SDK waits; cancellation alone cannot cancel
            // the SDK's unstructured concurrent-mic child task.
            _ = await setup?.result
            _ = await microphone?.result
            for control in controls { _ = await control.result }
            await telemetry?.value
            // A late connect may have completed after the first disconnect.
            await activeTransport?.disconnect()
        }
        if let state { onState(state) }
    }

    private func sendHangup(_ packet: Data, using transport: any CallMediaTransport) async {
        let deadline = hangupDeadline ?? ContinuousClock.now.advanced(by: hangupBudget)
        let send = Task {
            for index in 0..<2 {
                guard !Task.isCancelled, ContinuousClock.now < deadline else { return }
                timing.mark(index == 0 ? .hangupPublishStart : .hangupRetryStart)
                do {
                    try await transport.publishControl(packet)
                    timing.mark(index == 0 ? .hangupPublishEnd : .hangupRetryEnd)
                    return
                } catch {
                    timing.mark(index == 0 ? .hangupPublishError : .hangupRetryError)
                }
            }
        }
        // Separate from SDK publication: cancellation alone may not unblock an SDK wait.
        while !hangupAcknowledged, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(10))
        }
        send.cancel()
        timing.mark(.disconnect)
        // Best effort only; final telemetry must never extend the hangup budget.
        let finalTiming = Task {
            while supportsClientTiming, !Task.isCancelled, let batch = timing.nextBatch() {
                try? await transport.publishControl(batch)
            }
        }
        if hangupAcknowledged {
            let flushDeadline = min(deadline, ContinuousClock.now.advanced(by: .milliseconds(30)))
            while ContinuousClock.now < flushDeadline { try? await Task.sleep(for: .milliseconds(5)) }
        }
        finalTiming.cancel()
        transport.eventHandler = nil
        await transport.disconnect()
        await send.value
        await finalTiming.value
    }
}
