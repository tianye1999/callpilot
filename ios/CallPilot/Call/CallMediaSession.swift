import Foundation

@MainActor
private final class DirectSetupOutcome {
    var result: Result<Void, Error>?
}

/// Owns one call attempt, including setup, microphone publication and teardown.
@MainActor
final class CallMediaSession {
    typealias TransportFactory = @MainActor () -> any CallMediaTransport
    typealias DirectTransportFactory = @MainActor () -> any CallDirectMediaTransport
    typealias StateHandler = @MainActor (CallState) -> Void

    private var onState: StateHandler
    private let transportFactory: TransportFactory
    private let audio: CallAudioOwner
    private let directTransportFactory: DirectTransportFactory?
    private let directSetupMax: Duration
    private var directOfferId: String?
    private var directPreparationTask: Task<String?, Error>?
    private var directPreparedOffer: String?
    private var directPreparationFinished = false
    private var directPreparationRejection: HostedCloudError?
    private var directClaimTask: Task<HostedCallSession, Error>?
    private var directClaimClient: HostedCloudClient?
    private var directClaimID: String?
    private var ownerHangupRequested = false
    private var controlEndTask: Task<Void, Never>?
    private var directSetupFailure: Error?
    private var edgeCommitted = false
    private var directFallbackStarted = false
    private var directControlOpen = false
    private var lastDirectRtpStats: Data?
    private var directFallbackHandler: (() -> Void)?
    /// A8: the app-dialed call created with a direct offer (its same-call fallback key).
    private var outboundCallId: String?
    // A resource lease, not a network-speed threshold or a warm/cold heuristic.
    static let directSetupMax: Duration = .seconds(20)
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
    private var silentAudioLeaseTask: Task<Void, Error>?
    private var silentTeardownTask: Task<Void, Never>?
    private var silentPreconnectPending = false
    private var microphoneTask: Task<Void, Error>?
    private var controlTasks: [UUID: Task<Void, Error>] = [:]
    private var cleanupTask: Task<Void, Never>?
    static let hangupBudget: Duration = .milliseconds(2500)
    static let preconnectWaitTimeout: Duration = .seconds(8)
    /// 接听时有界等待直连预备 SDP 就绪的上限（≤1.2 s，见任务说明）。
    static let preconnectClaimWaitMax: Duration = .milliseconds(1200)
    private let hangupBudget: Duration
    private let preconnectClaimWaitBudget: Duration
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
            let acquiring = Task { try await acquireSilentAudioLease(for: attempt) }
            silentAudioLeaseTask = acquiring
            // Start the request before the lease's synchronous native audio preparation.
            timing.mark(.prepareStart)
            let candidate = try await client.prepareInboundOffer(offerId: offer.offerId)
            try checkCurrent(attempt)
            timing.mark(.prepareResponse)
            try await acquiring.value
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
                silentPreconnectPending = false
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

    private func closeSilentTransport(_ pending: Task<Void, Error>?) {
        preconnectReady = false
        silentPreconnectPending = false
        permissionReady = false
        preparation = nil
        let old = silentTransport
        silentTransport = nil
        if let old, transport === old { transport = nil }
        old?.eventHandler = nil
        pending?.cancel()
        // SDK disconnect/connect may stall. Neither may delay the cold claim.
        silentTeardownTask = Task {
            await old?.disconnect()
            _ = await pending?.result
            // Reap a late connect using only the detached connection, never the cold one.
            await old?.disconnect()
        }
    }

    init(onState: @escaping StateHandler,
         transportFactory: @escaping TransportFactory,
         audio: CallAudioOwner,
         hangupBudget: Duration = CallMediaSession.hangupBudget,
         timing: CallTiming = CallTiming(),
         directTransportFactory: DirectTransportFactory? = nil,
         directSetupMax: Duration = CallMediaSession.directSetupMax,
         preconnectClaimWaitBudget: Duration = CallMediaSession.preconnectClaimWaitMax) {
        self.onState = onState
        self.transportFactory = transportFactory
        self.audio = audio
        self.hangupBudget = hangupBudget
        self.preconnectClaimWaitBudget = preconnectClaimWaitBudget
        self.timing = timing
        self.directTransportFactory = directTransportFactory
        self.directSetupMax = directSetupMax
    }

    /// The same task survives Answer; no elapsed-ringing-time branch discards it.
    func startDirectPreparation(client: HostedCloudClient, offer: InboundOffer) async {
        beginDirectPreparation(client: client, offer: offer)
        _ = await directPreparationTask?.result
    }

    private func beginDirectPreparation(client: HostedCloudClient, offer: InboundOffer) {
        guard directTransportFactory != nil, directPreparationTask == nil,
              let attempt = beginAttempt(label: L10n.text("call.takeover.label"), mode: .callKit) else { return }
        directOfferId = offer.offerId
        directPreparationFinished = false
        let acquiring = Task { try await acquireSilentAudioLease(for: attempt) }
        silentAudioLeaseTask = acquiring
        let task = Task<String?, Error> {
            defer { directPreparationFinished = true }
            timing.mark(.prepareStart)
            let servers: [CallIceServer]
            do { servers = try await client.inboundIceServers(offerId: offer.offerId) }
            catch let error as HostedCloudError where error.statusCode == 401 || error.statusCode == 409 {
                directPreparationRejection = error
                throw error
            } catch { return nil }
            try checkCurrent(attempt)
            timing.mark(.prepareResponse)
            try await acquiring.value
            try checkCurrent(attempt)
            guard let directTransportFactory else { return nil }
            let connection = directTransportFactory()
            transport = connection
            installEvents(connection, attempt: attempt)
            timing.mark(.preconnectStart)
            do {
                let offer = try await connection.prepareDirect(servers: servers)
                try checkCurrent(attempt)
                directPreparedOffer = offer
                timing.mark(.preconnectReady)
                return offer
            } catch {
                try checkCurrent(attempt)
                detachDirect(connection)
                return nil
            }
        }
        directPreparationTask = task
    }

    /// 有界等待响铃期直连预备产出 SDP；未开始/已结束/超时都立即返回，不阻塞 claim 的冷路径。
    private func awaitDirectPreparation(upTo budget: Duration, attempt: UInt64) async throws {
        guard directPreparationTask != nil, !directPreparationFinished else { return }
        if directPreparedOffer != nil { return }
        let deadline = ContinuousClock.now.advanced(by: budget)
        while !directPreparationFinished && directPreparedOffer == nil {
            try checkCurrent(attempt)
            let remaining = ContinuousClock.now.duration(to: deadline)
            guard remaining > .zero else { return }
            try await Task.sleep(for: min(.milliseconds(20), remaining))
        }
    }

    private func installEvents(_ connection: any CallMediaTransport, attempt: UInt64) {
        connection.eventHandler = { [weak self, weak connection] event in
            guard let connection else { return }
            await self?.handleTransportEvent(event, from: connection, for: attempt)
        }
        connection.setSpeakerphone(speakerphoneEnabled)
    }

    private func detachDirect(_ connection: any CallDirectMediaTransport) {
        connection.finishRtpSampling()
        if let packet = connection.latestRtpStats, CallRtpStats.validatedObject(packet) != nil { lastDirectRtpStats = packet }
        directControlOpen = false
        connection.stopDirectAudio()
        connection.eventHandler = nil
        if transport === connection { transport = nil }
        // A stalled SDK disconnect must not hold the fallback connection hostage.
        Task { await connection.disconnect() }
    }

    private func startDirectTakeover(client: HostedCloudClient, offerId: String,
                                     prepareReservedAudio: (() async throws -> Void)?,
                                     onClaimed: @escaping () throws -> Void,
                                     onConnected: @escaping () -> Void) async -> Bool {
        // 只有响铃期已经在收集的直连预备才值得等；前台接听现起预备仍走「先 claim、后补交」。
        let hadInFlightPreparation = directPreparationTask != nil
        if directPreparationTask == nil {
            // No push prewarm (for example, a foreground offer); begin immediately.
            beginDirectPreparation(client: client, offer: InboundOffer(offerId: offerId, expiresAt: 0))
        }
        guard !claimStarted, !isStopped, directOfferId == offerId else { return false }
        claimStarted = true
        connectedHandler = onConnected
        let attempt = generation
        let task = Task {
            try checkCurrent(attempt)
            if let directPreparationRejection { throw directPreparationRejection }
            // 预备仍在收集中：有界等它产出 SDP，随 claim 一次提交；超时/失败/未开始都不等。
            if hadInFlightPreparation, directPreparedOffer == nil, !directPreparationFinished {
                try await awaitDirectPreparation(upTo: preconnectClaimWaitBudget, attempt: attempt)
                try checkCurrent(attempt)
            }
            timing.startClaim()
            directClaimClient = client
            let claim = Task {
                // A canceled UI/setup must still learn the CAS outcome so an
                // explicit hangup can end that exact winner, never another call.
                let session = try await client.claimInboundOffer(offerId: offerId, timing: timing,
                                                                 sdpOffer: directPreparedOffer, directRequested: true)
                directClaimID = session.sessionId
                endReservedClaimIfRequested()
                return session
            }
            directClaimTask = claim
            let session = try await claim.value
            timing.mark(.claimResponse)
            try checkCurrent(attempt)
            preparedSession = session
            claimAccepted = true
            // Ownership reaches Edge before any TURN, gather, or audio-lease wait.
            // CallKit configuration still requires this attempt's audio lease.
            try await prepareReservedAudio?()
            try checkCurrent(attempt)
            try await silentAudioLeaseTask?.value
            try checkCurrent(attempt)
            directFallbackHandler = { [weak self] in
                guard let self, isCurrent(attempt), !edgeCommitted, !directFallbackStarted else { return }
                directFallbackHandler = nil
                let fallback = Task { try await self.connectFallback(client: client, session: session, attempt: attempt) }
                setupTask = fallback
                Task { await self.finishSetup(fallback, attempt: attempt) }
            }
            try onClaimed()
            try checkCurrent(attempt)
            var mediaSession = session
            if session.sdpPending {
                do {
                    if let offer = try await directPreparationTask?.value {
                        try checkCurrent(attempt)
                        timing.mark(.sdpSubmitStart)
                        mediaSession = try await client.completeInboundSDP(offerId: offerId,
                            claimId: session.sessionId, sdpOffer: offer)
                        timing.mark(.sdpSubmitResponse)
                        try checkCurrent(attempt)
                        preparedSession = mediaSession
                    }
                } catch let error as HostedCloudError where error.statusCode == 401 || error.statusCode == 409 {
                    throw error
                } catch {
                    try checkCurrent(attempt)
                    timing.mark(.coldFallback)
                }
            }
            try activateAudioIfReady()
            if directFallbackStarted { return }
            if let answer = mediaSession.sdpAnswer, let direct = transport as? any CallDirectMediaTransport {
                do {
                    timing.mark(.connectStart)
                    try await connectDirect(direct, answer: answer, attempt: attempt)
                    timing.mark(.connectReturn)
                    try checkCurrent(attempt)
                    roomConnected = true
                    timing.mark(.roomConnected)
                    startMicrophoneIfReady(for: attempt)
                    scheduleTiming()
                    return
                } catch {
                    try checkCurrent(attempt)
                    timing.mark(.coldFallback)
                    detachDirect(direct)
                }
            } else if let direct = transport as? any CallDirectMediaTransport {
                detachDirect(direct)
            }
            try await connectFallback(client: client, session: session, attempt: attempt)
        }
        setupTask = task
        await finishSetup(task, attempt: attempt)
        return isCurrent(attempt)
    }

    private func connectFallback(client: HostedCloudClient, session: HostedCallSession,
                                 attempt: UInt64) async throws {
        try checkCurrent(attempt)
        guard !edgeCommitted, !directFallbackStarted, let offerId = directOfferId else { return }
        directFallbackStarted = true
        directFallbackHandler = nil
        directPreparationTask?.cancel()
        if let direct = transport as? any CallDirectMediaTransport { detachDirect(direct) }
        microphoneTask?.cancel()
        microphoneStarted = false
        roomConnected = false
        audioActivated = false
        // A successful CAS is never repeated. The fallback route keeps its fence.
        let fallback = session.directAttempted
            ? try await client.fallbackInboundLiveKit(offerId: offerId, claimId: session.sessionId,
                                                     clientTiming: timing.httpBatches(reservingBytes: CallRtpStats.maxBytes),
                                                     clientRtpStats: lastDirectRtpStats)
            : session
        try checkCurrent(attempt)
        if mode == .callKit { try audio.configureCallKitSession(ownerID) }
        try activateAudioIfReady()
        try await connect(fallback, for: attempt)
    }

    private func connectDirect(_ connection: any CallDirectMediaTransport, answer: String,
                               attempt: UInt64) async throws {
        let outcome = DirectSetupOutcome()
        directSetupFailure = nil
        let operation = Task {
            do { try await connection.connectDirect(answer: answer); outcome.result = .success(()) }
            catch { outcome.result = .failure(error) }
        }
        defer { operation.cancel() }
        let deadline = ContinuousClock.now.advanced(by: directSetupMax)
        while outcome.result == nil {
            try checkCurrent(attempt)
            if let directSetupFailure { throw directSetupFailure }
            guard ContinuousClock.now < deadline else { throw CallAudioError.answerExpired }
            try await Task.sleep(for: .milliseconds(20))
        }
        try outcome.result?.get()
    }

    func startOutbound(client: HostedCloudClient, edgeId: String, number: String, direct: Bool = false) async {
        guard let attempt = beginAttempt(label: number, mode: .standalone) else { return }
        timing.mark(.dialStart)
        let task = Task {
            try await audio.acquire(ownerID, mode: .standalone)
            try checkCurrent(attempt)
            let packet = try CallSignaling.encodeDial(number: number, idempotencyKey: UUID().uuidString)
            if direct, directTransportFactory != nil {
                if try await startOutboundDirect(client: client, edgeId: edgeId, packet: packet, attempt: attempt) {
                    return
                }
            }
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

    /// A8: app-dialed call over direct WebRTC with one same-call LiveKit fallback before dialing.
    /// Returns false while no call exists yet (direct unavailable), so the caller keeps LiveKit.
    private func startOutboundDirect(client: HostedCloudClient, edgeId: String, packet: Data,
                                     attempt: UInt64) async throws -> Bool {
        guard let directTransportFactory else { return false }
        let connection = directTransportFactory()
        transport = connection
        installEvents(connection, attempt: attempt)
        connection.captureAudioDiagnostic(stage: .audioLeaseEnd)
        let servers: [CallIceServer]
        do { servers = try await client.outboundIceServers(edgeId: edgeId) }
        catch is CancellationError { throw CancellationError() }
        catch { detachDirect(connection); return false }
        try checkCurrent(attempt)
        timing.mark(.preconnectStart)
        let offer: String
        do { offer = try await connection.prepareDirect(servers: servers) }
        catch {
            try checkCurrent(attempt)
            detachDirect(connection)
            return false
        }
        try checkCurrent(attempt)
        timing.mark(.preconnectReady)
        connection.captureAudioDiagnostic(stage: .preconnectReady)
        timing.mark(.claimStart)
        let call = try await client.createDirectCall(edgeId: edgeId, sdpOffer: offer)
        timing.mark(.claimResponse)
        try checkCurrent(attempt)
        outboundCallId = call.callId
        pendingDialPacket = packet
        onState(.waitingMedia(label: activeLabel ?? ""))
        directFallbackHandler = { [weak self] in
            guard let self, isCurrent(attempt), pendingDialPacket != nil, !directFallbackStarted else { return }
            directFallbackHandler = nil
            let fallback = Task { try await self.connectOutboundFallback(client: client, attempt: attempt) }
            setupTask = fallback
            Task { await self.finishSetup(fallback, attempt: attempt) }
        }
        if let answer = call.sdpAnswer {
            do {
                timing.mark(.connectStart)
                try await connectDirect(connection, answer: answer, attempt: attempt)
                timing.mark(.connectReturn)
                try checkCurrent(attempt)
                // No CallKit activation exists for app-dialed calls.
                try connection.activateStandaloneAudio()
                audioActivated = true
                roomConnected = true
                timing.mark(.roomConnected)
                scheduleTiming()
                return true
            } catch {
                try checkCurrent(attempt)
                timing.mark(.coldFallback)
            }
        } else if !call.directAttempted {
            // The relay started a plain LiveKit session (older Edge): poll it as before.
            directFallbackHandler = nil
            detachDirect(connection)
            let session = try await client.awaitCallSession(edgeId: edgeId, call: call)
            try checkCurrent(attempt)
            preparedSession = session
            try await connect(session, for: attempt)
            return true
        }
        try await connectOutboundFallback(client: client, attempt: attempt)
        return true
    }

    private func connectOutboundFallback(client: HostedCloudClient, attempt: UInt64) async throws {
        try checkCurrent(attempt)
        guard !directFallbackStarted, pendingDialPacket != nil, let callId = outboundCallId else { return }
        directFallbackStarted = true
        directFallbackHandler = nil
        if let direct = transport as? any CallDirectMediaTransport { detachDirect(direct) }
        roomConnected = false
        audioActivated = false
        let session = try await client.fallbackCallLiveKit(callId: callId)
        try checkCurrent(attempt)
        preparedSession = session
        try await connect(session, for: attempt)
    }

    /// Edge waits for our microphone subscription before committing phone-first takeover.
    @discardableResult
    func startTakeover(
        client: HostedCloudClient,
        offerId: String,
        usesCallKit: Bool = false,
        prepareReservedAudio: (() async throws -> Void)? = nil,
        onClaimed: @escaping () throws -> Void = {},
        onConnected: @escaping () -> Void
    ) async -> Bool {
        if directTransportFactory != nil, usesCallKit {
            return await startDirectTakeover(client: client, offerId: offerId,
                                             prepareReservedAudio: prepareReservedAudio,
                                             onClaimed: onClaimed, onConnected: onConnected)
        }
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
        // Only a connection already started at answer time is worth waiting for.
        let shouldWaitForPreconnect = silentTask != nil && silentTransport != nil
        claimStarted = true
        connectedHandler = onConnected
        let task = Task {
            if shouldWaitForPreconnect { try await waitForSilentPreconnect(for: attempt) }
            try checkCurrent(attempt)
            let candidate = preconnectReady ? preparation : nil
            if silentOfferId != nil && candidate == nil {
                timing.mark(.coldFallback)
                closeSilentTransport(silentTask)
                try checkCurrent(attempt)
            }
            if !audioLeaseAcquired {
                if let silentAudioLeaseTask {
                    // Share the in-flight lease, not the cancelled silent SDK setup.
                    try await silentAudioLeaseTask.value
                } else {
                    timing.mark(.audioLeaseStart)
                    try await audio.acquire(ownerID, mode: mode)
                    audioLeaseAcquired = true
                    timing.mark(.audioLeaseEnd)
                }
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
                closeSilentTransport(silentTask)
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
        if directTransportFactory != nil && !directFallbackStarted && transport == nil { return }
        if let direct = transport as? any CallDirectMediaTransport {
            do {
                try direct.activateDirectAudio()
                try audio.activate(ownerID)
            }
            catch {
                timing.mark(.audioEngineFailed)
                scheduleTiming()
                directFallbackHandler?()
                return
            }
        }
        else { try audio.activate(ownerID) }
        timing.mark(.activateReturn)
        audioActivated = true
        timing.mark(.audioActivated)
        scheduleTiming()
        startMicrophoneIfReady(for: generation)
    }

    private func connect(_ session: HostedCallSession, for attempt: UInt64) async throws {
        edgeIdentity = session.edgeIdentity
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
            guard let self, isCurrent(attempt), transport === activeTransport else { return }
            if case .failure = result {
                if activeTransport is any CallDirectMediaTransport, !edgeCommitted {
                    directFallbackHandler?()
                    return
                }
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
                    if error.code == "OFFER_ANSWERED_ELSEWHERE" { finishOwnerDecision("answered_elsewhere") }
                    else if error.code == "OFFER_OWNER_DECLINED" { finishOwnerDecision("owner_declined") }
                    else { fail(reason: error.message, code: error.code) }
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
        edgeCommitted = true
        (transport as? any CallDirectMediaTransport)?.finishRtpSampling()
        directFallbackHandler = nil
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
        if directTransportFactory != nil {
            ownerHangupRequested = true
            endReservedClaimIfRequested()
        }
        let id = UUID().uuidString
        let packet = CallSignaling.encodeHangup(commandId: id)
        hangupCommandID = id
        hangupPacket = packet
        hangupDeadline = ContinuousClock.now.advanced(by: hangupBudget)
        timing.mark(.hangupIntent)
        terminate(state: .ended(label: activeLabel ?? "", reason: "local_hangup"), hangup: packet)
        return packet
    }

    private func endReservedClaimIfRequested() {
        guard ownerHangupRequested, controlEndTask == nil, let client = directClaimClient,
              let offerId = directOfferId, let claimId = directClaimID else { return }
        if let direct = transport as? any CallDirectMediaTransport {
            direct.finishRtpSampling()
            if let packet = direct.latestRtpStats, CallRtpStats.validatedObject(packet) != nil { lastDirectRtpStats = packet }
        }
        // Independent of media teardown and its cancellation. The server checks
        // the device/claim fence even when RTP and the data channel never opened.
        controlEndTask = Task {
            _ = try? await client.endInboundClaim(offerId: offerId, claimId: claimId,
                                                clientTiming: timing.httpBatches(reservingBytes: CallRtpStats.maxBytes),
                                                clientRtpStats: lastDirectRtpStats)
        }
    }

    func stop() async {
        terminate(state: nil)
        await cleanupTask?.value
    }

    func cancelSilently() {
        terminate(state: nil)
    }

    func finishOwnerDecision(_ state: String) {
        terminate(state: .ended(label: activeLabel ?? "", reason: state == "answered_elsewhere"
            ? L10n.text("call.answered_elsewhere") : state == "owner_declined"
            ? L10n.text("call.owner_declined") : "remote_ended"))
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
        case let .participantLeft(identity):
            if identity == edgeIdentity {
                terminate(state: .ended(label: activeLabel ?? "", reason: "edge_left"))
            }
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
            if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               object["type"] as? String == "ended" {
                let reason = (object["reason"] as? String).map { String($0.prefix(64)) } ?? "call_ended"
                terminate(state: .ended(label: activeLabel ?? "", reason: reason))
                return
            }
            if connection is any CallDirectMediaTransport, !edgeCommitted,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               object["type"] as? String == "media_failed" {
                directFallbackHandler?()
                return
            }
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
            // After an outbound dial the call cannot switch transports; a lost peer ends it.
            if connection is any CallDirectMediaTransport, !edgeCommitted,
               outboundCallId == nil || pendingDialPacket != nil {
                directSetupFailure = CallAudioError.answerExpired
                if roomConnected { directFallbackHandler?() }
                return
            }
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
            if mark == .directControlReady {
                directControlOpen = true
                supportsClientTiming = true
                if let packet = lastDirectRtpStats { try? await connection.publishControl(packet) }
            }
            scheduleTiming()
        case let .audioDiagnostic(snapshot, stage, instant):
            timing.recordAudioDiagnostic(snapshot, stage: stage, at: instant)
            scheduleTiming()
            // Preserve the evidence before a following startup failure detaches
            // this transport. Pre-channel observations stay in the bounded queue.
            if directControlOpen { await timingTask?.value }
        case let .audioEngine(snapshot, failed):
            timing.recordAudioEngine(snapshot)
            timing.mark(failed ? .audioEngineFailed : .audioEngineRunning)
            scheduleTiming()
            if failed {
                if edgeCommitted {
                    // RTP can commit while the asynchronous ADM readback is
                    // pending. This claim can no longer switch transports.
                    fail(reason: L10n.text("call.audio.failed"), code: "AUDIO_ENGINE_FAILED")
                } else {
                    directFallbackHandler?()
                }
            }
        case let .rtpStats(packet):
            guard connection is any CallDirectMediaTransport,
                  CallRtpStats.validatedObject(packet) != nil else { return }
            lastDirectRtpStats = packet
            if directControlOpen { try? await connection.publishControl(packet) }
        }
    }

    private func scheduleTiming() {
        guard !isStopped, (roomConnected || directControlOpen), supportsClientTiming, timingTask == nil,
              let transport else { return }
        timingTask = Task { [weak self] in
            defer { self?.timingTask = nil }
            guard let self else { return }
            if !directControlOpen {
                do { try await Task.sleep(for: .milliseconds(30)) } catch { return }
            }
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
        directPreparationTask?.cancel()
        let directPreparation = directPreparationTask
        directPreparationTask = nil
        (transport as? any CallDirectMediaTransport)?.stopDirectAudio()
        let silentSetup = silentSetupTask
        let silentAudioLease = silentAudioLeaseTask
        let silentTeardown = silentTeardownTask
        let microphone = microphoneTask
        let controls = Array(controlTasks.values)
        let telemetry = timingTask
        telemetry?.cancel()
        timingTask = nil
        setup?.cancel()
        silentSetup?.cancel()
        silentAudioLease?.cancel()
        microphone?.cancel()
        controls.forEach { $0.cancel() }
        setupTask = nil
        directFallbackHandler = nil
        silentSetupTask = nil
        silentAudioLeaseTask = nil
        silentTeardownTask = nil
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
        if directTransportFactory != nil,
           activeTransport == nil || activeTransport is any CallDirectMediaTransport {
            // Direct audio is synchronously revoked above. Reaping a stuck peer
            // must not hold the process-wide audio lease or a subsequent call.
            cleanupTask = audio.release(ownerID) {
                Task {
                    if let hangup, let activeTransport { await self.sendHangup(hangup, using: activeTransport) }
                    activeTransport?.eventHandler = nil
                    await activeTransport?.disconnect()
                    _ = await directPreparation?.result
                    await activeTransport?.disconnect()
                }
                self.transport = nil
            }
            if let state { onState(state) }
            return
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
            _ = await directPreparation?.result
            _ = await silentSetup?.result
            _ = await silentAudioLease?.result
            await silentTeardown?.value
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
