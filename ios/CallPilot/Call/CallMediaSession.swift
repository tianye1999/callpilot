import Foundation

/// Owns one call attempt, including setup, microphone publication and teardown.
@MainActor
final class CallMediaSession {
    typealias TransportFactory = @MainActor () -> any CallMediaTransport
    typealias StateHandler = @MainActor (CallState) -> Void

    private var onState: StateHandler
    private let transportFactory: TransportFactory
    private let audio: CallAudioOwner
    private let ownerID = UUID()
    private var connectedHandler: (() -> Void)?
    private var transport: (any CallMediaTransport)?
    private var silentTransport: (any CallMediaTransport)?
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
    private var activationRequested = false
    private var microphoneStarted = false
    private var setupTask: Task<Void, Error>?
    // Answer setup replaces setupTask; retain silent work for cancellation and drain.
    private var silentSetupTask: Task<Void, Error>?
    private var silentPreconnectPending = false
    private var microphoneTask: Task<Void, Error>?
    private var controlTasks: [UUID: Task<Void, Error>] = [:]
    private var cleanupTask: Task<Void, Never>?
    static let hangupBudget: Duration = .milliseconds(2500)
    static let preconnectWaitTimeout: Duration = .seconds(3)
    private let hangupBudget: Duration
    let timing: CallTiming
    private var timingTask: Task<Void, Never>?
    private var supportsClientTiming = false
    private var hangupPacket: Data?
    private var hangupCommandID: String?
    private var hangupAcknowledged = false
    private var hangupDeadline: ContinuousClock.Instant?
    private var silentOfferId: String?
    private var preparation: InboundPreparation?
    private var preconnectReady = false
    private var audioLeaseAcquired = false
    private var claimStarted = false
    private var claimAccepted = false
    private var permissionReady = false
    private var reusingTransport = false
    private var edgeIdentity: String?
    var onSilentDisconnect: (() -> Void)?

    func setStateHandler(_ handler: @escaping StateHandler) { onState = handler }

    func startSilentPreconnect(client: HostedCloudClient, offer: InboundOffer) async {
        guard let attempt = beginAttempt(label: L10n.text("call.takeover.label"), mode: .callKit) else { return }
        silentOfferId = offer.offerId
        silentPreconnectPending = true
        let task = Task {
            defer { silentPreconnectPending = false }
            try checkCurrent(attempt)
            async let acquiring: Void = acquireSilentAudioLease(for: attempt)
            // Start the request before the lease's synchronous native audio preparation.
            timing.mark(.prepareStart)
            let candidate = try await client.prepareInboundOffer(offerId: offer.offerId)
            try checkCurrent(attempt)
            timing.mark(.prepareResponse)
            try await acquiring
            try checkCurrent(attempt)
            let connection = transportFactory()
            // The factory is synchronous, but may reenter session cancellation.
            do { try checkCurrent(attempt) }
            catch {
                await connection.disconnect()
                throw error
            }
            silentTransport = connection
            preparation = candidate
            transport = connection
            connection.eventHandler = { [weak self, weak connection] event in
                guard let connection else { return }
                await self?.handleTransportEvent(event, from: connection, for: attempt)
            }
            timing.mark(.preconnectStart)
            do {
                try await connection.connectSilent(url: candidate.session.livekitURL, token: candidate.session.token)
                try checkCurrent(attempt)
            } catch {
                connection.eventHandler = nil
                if transport === connection { transport = nil }
                await connection.disconnect()
                throw error
            }
            preconnectReady = true
            timing.mark(.preconnectReady)
        }
        silentSetupTask = task
        setupTask = task
        _ = await task.result
        silentSetupTask = nil
        // Failure is silent. Answer can still use the cold path, with the same lease.
    }

    private func waitForSilentPreconnect(for attempt: UInt64) async throws {
        let deadline = ContinuousClock.now.advanced(by: Self.preconnectWaitTimeout)
        // Do not await the SDK task directly: it may ignore cancellation.
        while silentPreconnectPending && !preconnectReady {
            try checkCurrent(attempt)
            let remaining = ContinuousClock.now.duration(to: deadline)
            guard remaining > .zero else { break }
            try await Task.sleep(for: min(.milliseconds(20), remaining))
        }
        try checkCurrent(attempt)
    }

    private func acquireSilentAudioLease(for attempt: UInt64) async throws {
        try checkCurrent(attempt)
        timing.mark(.audioLeaseStart)
        try await audio.acquire(ownerID, mode: .callKit)
        // Record ownership even if cancellation arrived as acquire returned.
        audioLeaseAcquired = true
        timing.mark(.audioLeaseEnd)
        try checkCurrent(attempt)
    }

    private func closeSilentTransport(_ pending: Task<Void, Error>?) async {
        preconnectReady = false
        permissionReady = false
        preparation = nil
        pending?.cancel()
        let old = silentTransport
        if let old, transport === old { transport = nil }
        old?.eventHandler = nil
        await old?.disconnect()
        _ = await pending?.result
        // Re-read after draining: clean up the actual silent connection, never a cold one.
        let settled = silentTransport
        if let settled, transport === settled { transport = nil }
        settled?.eventHandler = nil
        await settled?.disconnect()
        silentTransport = nil
    }

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
        guard !claimStarted, !isStopped else { return false }
        let attempt: UInt64
        let silentTask: Task<Void, Error>?
        if silentOfferId == offerId, usesCallKit {
            attempt = generation
            silentTask = setupTask
        } else {
            guard let value = beginAttempt(label: L10n.text("call.takeover.label"),
                                           mode: usesCallKit ? .callKit : .standalone) else { return false }
            attempt = value
            silentTask = nil
        }
        claimStarted = true
        connectedHandler = onConnected
        let task = Task {
            if silentTask != nil { try await waitForSilentPreconnect(for: attempt) }
            try checkCurrent(attempt)
            let candidate = preconnectReady ? preparation : nil
            if silentOfferId != nil && candidate == nil {
                timing.mark(.coldFallback)
                await closeSilentTransport(silentTask)
                try checkCurrent(attempt)
            }
            if !audioLeaseAcquired {
                timing.mark(.audioLeaseStart)
                try await audio.acquire(ownerID, mode: mode)
                audioLeaseAcquired = true
                timing.mark(.audioLeaseEnd)
            }
            try checkCurrent(attempt)
            timing.startClaim()
            let session = try await client.claimInboundOffer(offerId: offerId, timing: timing, preparation: candidate)
            timing.mark(.claimResponse)
            try checkCurrent(attempt)
            preparedSession = session
            claimAccepted = true
            if session.mode == "reuse", candidate != nil, preconnectReady {
                reusingTransport = true
                edgeIdentity = session.edgeIdentity
                roomConnected = true
                timing.mark(.transportAdopted)
                try onClaimed()
                try checkCurrent(attempt)
                try activateAudioIfReady()
                startMicrophoneIfReady(for: attempt)
                return
            }
            if candidate != nil {
                timing.mark(.coldFallback)
                await closeSilentTransport(silentTask)
                try checkCurrent(attempt)
            }
            silentOfferId = nil
            // Configure + fulfill before starting any media connection.
            try onClaimed()
            try checkCurrent(attempt)
            try activateAudioIfReady()
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
        activationRequested = true
        try activateAudioIfReady()
    }

    private func activateAudioIfReady() throws {
        guard activationRequested, claimAccepted, !reusingTransport || permissionReady else { return }
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
        activeTransport.eventHandler = { [weak self, weak activeTransport] event in
            guard let activeTransport else { return }
            await self?.handleTransportEvent(event, from: activeTransport, for: attempt)
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
              claimAccepted, (!reusingTransport || permissionReady),
              !microphoneStarted, let activeTransport = transport else { return }
        microphoneStarted = true
        let task = Task {
            try checkCurrent(attempt)
            if reusingTransport, let edgeIdentity {
                try await activeTransport.subscribeToEdge(edgeIdentity)
                try checkCurrent(attempt)
            }
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

    func cancelSilently() {
        terminate(state: nil)
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

    private func handleTransportEvent(
        _ event: CallMediaTransportEvent, from connection: any CallMediaTransport, for attempt: UInt64
    ) async {
        guard transport === connection else { return }
        if case let .data(data, topic) = event, topic == CallPilotTopic.status,
           let id = CallSignaling.decodeHangupAck(data), id == hangupCommandID {
            hangupAcknowledged = true
            timing.mark(.hangupAck)
            return
        }
        guard isCurrent(attempt) else { return }
        switch event {
        case let .permissionsReady(ready):
            permissionReady = ready
            if ready { timing.mark(.permissionReady) }
            do { try activateAudioIfReady() }
            catch { fail(reason: L10n.text("call.audio.failed"), code: "AUDIO_ACTIVATION_FAILED") }
            startMicrophoneIfReady(for: attempt)
        case .tracksChanged:
            if reusingTransport, claimAccepted, permissionReady, audioActivated,
               let edgeIdentity, let transport {
                try? await transport.subscribeToEdge(edgeIdentity)
            }
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
            if silentOfferId != nil && !claimAccepted {
                preconnectReady = false
                silentPreconnectPending = false
                silentSetupTask?.cancel()
                if !claimStarted {
                    onSilentDisconnect?()
                    terminate(state: nil)
                }
                return
            }
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
        let silentSetup = silentSetupTask
        let microphone = microphoneTask
        let controls = Array(controlTasks.values)
        let telemetry = timingTask
        telemetry?.cancel()
        timingTask = nil
        setup?.cancel()
        silentSetup?.cancel()
        microphone?.cancel()
        controls.forEach { $0.cancel() }
        setupTask = nil
        silentSetupTask = nil
        microphoneTask = nil
        controlTasks.removeAll()
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        let activeTransport = transport
        if hangup == nil {
            activeTransport?.eventHandler = nil
            transport = nil
        }
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
            _ = await silentSetup?.result
            _ = await microphone?.result
            for control in controls { _ = await control.result }
            await telemetry?.value
            // A late connect may have completed after the first disconnect.
            await activeTransport?.disconnect()
            self.transport = nil
            self.silentTransport = nil
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
