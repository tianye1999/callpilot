import AVFAudio
import Foundation
import LiveKitWebRTC
import OSLog

/// One peer per inbound claim. The audio device stays disabled while gathering.
@MainActor
final class WebRtcDirectTransport: CallDirectMediaTransport {
    private let owner = UUID()
    private let delegate = DirectPeerDelegate()
    private var factory: LKRTCPeerConnectionFactory?
    private var peer: LKRTCPeerConnection?
    private var channel: LKRTCDataChannel?
    private var microphone: LKRTCAudioTrack?
    private var closed = false
    private var audioRevoked = false
    private var audioActive = false
    private var speakerPreferred = false
    /// A8: an app-dialed call owns its route; CallKit owns it for answered calls.
    private var standaloneAudio = false
    private var transportConnected = false
    private var audioVerificationTask: Task<Void, Never>?
    private var audioObservationTask: Task<Void, Never>?
    private var audioNotifications: [NSObjectProtocol] = []
    private let audioLog = Logger(subsystem: "ai.bondings.callpilot.sim", category: "DirectAudio")
    private(set) var latestRtpStats: Data?
    private var latestRtpSequence = 0
    private lazy var rtpSampler = CallRtpSampler { [weak self] sequence, elapsed in
        self?.requestRtpStats(sequence: sequence, elapsedMs: elapsed)
    }
    var eventHandler: ((CallMediaTransportEvent) async -> Void)?

    init() {
        delegate.relay = CallMediaEventRelay { [weak self] event in
            guard let self, !closed else { return }
            await eventHandler?(event)
        }
    }

    func prepareDirect(servers: [CallIceServer]) async throws -> String {
        guard !closed, !audioRevoked, peer == nil else { throw DirectMediaError.closed }
        // Preparation already holds the exclusive audio lease. An enabled
        // sender must still never capture during ringing or before CallKit.
        try WebRtcAudioBridge.preparePrivacyGate(owner)
        LKRTCInitializeSSL()
        // Keep the reference implementation's platform AudioUnit ADM. The
        // explicit initializer also exposes its diagnostic/control wrapper.
        let factory = LKRTCPeerConnectionFactory(audioDeviceModuleType: .platformDefault,
            bypassVoiceProcessing: false, encoderFactory: nil, decoderFactory: nil,
            audioProcessingModule: nil)
        self.factory = factory
        let configuration = LKRTCConfiguration()
        configuration.sdpSemantics = .unifiedPlan
        configuration.continualGatheringPolicy = .gatherOnce
        configuration.iceServers = servers.map {
            LKRTCIceServer(urlStrings: $0.urls, username: $0.username, credential: $0.credential)
        }
        let constraints = LKRTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        guard let peer = factory.peerConnection(with: configuration, constraints: constraints, delegate: delegate) else {
            throw DirectMediaError.unavailable
        }
        self.peer = peer
        let track = factory.audioTrack(with: factory.audioSource(with: constraints), trackId: "phone-uplink")
        track.isEnabled = true
        microphone = track
        peer.add(track, streamIds: ["callpilot"])
        guard let channel = peer.dataChannel(forLabel: CallPilotTopic.control,
                                             configuration: LKRTCDataChannelConfiguration()) else {
            throw DirectMediaError.unavailable
        }
        self.channel = channel
        channel.delegate = delegate
        let offer: LKRTCSessionDescription = try await withCheckedThrowingContinuation { continuation in
            peer.offer(for: constraints) { description, error in
                if let description {
                    continuation.resume(returning: description)
                } else { continuation.resume(throwing: error ?? DirectMediaError.unavailable) }
            }
        }
        try ensureOpen()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setLocalDescription(offer) { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            }
        }
        // An event (relay candidate or gather complete), not a warm/cold delay.
        // The cap bounds an SDK that never emits either event.
        let gatherDeadline = ContinuousClock.now.advanced(by: .seconds(20))
        try await delegate.gathered.wait(maximum: .seconds(20))
        // A delegate notification is not sufficient if the SDK has not yet
        // reflected that relay candidate in localDescription. Await completion
        // within the same watchdog, never send an accidentally candidate-free SDP.
        if peer.iceGatheringState != .complete,
           peer.localDescription?.sdp.contains(" typ relay") != true {
            try await delegate.gatherComplete.wait(maximum: max(.zero, ContinuousClock.now.duration(to: gatherDeadline)))
        }
        try ensureOpen()
        guard let local = peer.localDescription, local.sdp.utf8.count <= 16 * 1024 else {
            throw DirectMediaError.invalidSDP
        }
        return local.sdp
    }

    func connectDirect(answer: String) async throws {
        try ensureOpen()
        try WebRtcAudioBridge.acquire(owner)
        if audioActive { try WebRtcAudioBridge.activate(owner) }
        guard let peer, !answer.isEmpty, answer.utf8.count <= 16 * 1024 else { throw DirectMediaError.invalidSDP }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setRemoteDescription(LKRTCSessionDescription(type: .answer, sdp: answer)) { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            }
        }
        try await delegate.connected.wait()
        transportConnected = true
        rtpSampler.start()
        verifyActivatedAudioIfReady()
        try await delegate.channelOpen.wait()
        try ensureOpen()
    }

    func activateDirectAudio() throws {
        try ensureOpen()
        guard !audioRevoked else { throw DirectMediaError.audioInactive }
        try WebRtcAudioBridge.acquire(owner)
        try WebRtcAudioBridge.activate(owner)
        audioActive = true
        verifyActivatedAudioIfReady()
    }

    /// A8: an app-dialed call owns the audio session itself instead of CallKit.
    func activateStandaloneAudio() throws {
        try ensureOpen()
        guard !audioRevoked else { throw DirectMediaError.audioInactive }
        standaloneAudio = true
        try configureStandaloneSession()
        try activateDirectAudio()
        captureAudioDiagnostic(stage: .activateReturn)
        applyStandaloneRoute()
        let activatedAt = ContinuousClock.now
        audioObservationTask = Task { [weak self] in
            for delay: Duration in [.milliseconds(300), .seconds(1), .seconds(3)] {
                do { try await Task.sleep(until: activatedAt.advanced(by: delay), clock: .continuous) }
                catch { return }
                guard let self, !closed, !audioRevoked else { return }
                captureAudioDiagnostic(stage: .audioEngineRunning)
            }
        }
    }

    private func configureStandaloneSession() throws {
        let rtc = LKRTCAudioSession.sharedInstance()
        let session = AVAudioSession.sharedInstance()
        do {
            rtc.lockForConfiguration()
            defer { rtc.unlockForConfiguration() }
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: [])
        } catch {
            audioLog.error("setCategory failed: \(String(describing: error), privacy: .public)")
            logAudio("setCategory failure")
            captureAudioDiagnostic(stage: .configureStart)
            throw error
        }
        // ADM getters can synchronously enter the worker thread. Never read
        // them while holding the session lock that worker may need itself.
        logAudio("setCategory success")
        captureAudioDiagnostic(stage: .configureStart)
        do {
            rtc.lockForConfiguration()
            defer { rtc.unlockForConfiguration() }
            try session.setActive(true)
        } catch {
            audioLog.error("setActive failed: \(String(describing: error), privacy: .public)")
            logAudio("setActive failure")
            captureAudioDiagnostic(stage: .configureEnd)
            throw error
        }
        logAudio("setActive success")
        captureAudioDiagnostic(stage: .configureEnd)
    }

    private func applyStandaloneRoute() {
        guard standaloneAudio, !closed, !audioRevoked else { return }
        try? AVAudioSession.sharedInstance().overrideOutputAudioPort(speakerPreferred ? .speaker : .none)
    }

    func stopDirectAudio() {
        finishRtpSampling()
        audioVerificationTask?.cancel()
        audioObservationTask?.cancel()
        audioNotifications.forEach { NotificationCenter.default.removeObserver($0) }
        audioNotifications.removeAll()
        audioRevoked = true
        microphone?.isEnabled = false
        delegate.revokeAudio()
        audioActive = false
        WebRtcAudioBridge.release(owner)
        // Stop this factory's device as well as the shared CallKit gate. A later
        // owner may enable the shared session while this peer is being reaped.
        if let module = factory?.audioDeviceModule {
            _ = module.stopRecording()
            _ = module.stopPlayout()
        }
    }

    func connect(url: String, token: String, enableMicrophone: Bool) async throws {
        throw DirectMediaError.unavailable
    }

    func setMicrophone(enabled: Bool) async throws {
        try ensureOpen()
        guard !enabled || audioActive else { throw DirectMediaError.audioInactive }
        microphone?.isEnabled = enabled
    }

    private func verifyActivatedAudioIfReady() {
        guard audioActive, transportConnected, !audioRevoked, !closed,
              audioVerificationTask == nil, let module = factory?.audioDeviceModule else { return }
        audioVerificationTask = Task { [weak self] in
            await self?.verifyActivatedAudio(module)
        }
    }

    private func verifyActivatedAudio(_ module: LKRTCAudioDeviceModule) async {
        // AudioUnit starts naturally via the CallKit/manual-audio gate. Only
        // repair a failed readback; do not replace that working primary path.
        let result: CallAudioEngineSnapshot
        do {
            result = try await CallDirectAudioStartup.verify(snapshot: { self.sessionSnapshot() }, repair: {
                if self.standaloneAudio {
                    guard !Task.isCancelled, !self.audioRevoked, !self.closed else { return false }
                    // A recording flag alone does not prove an input-capable session.
                    // Stop both sides so a stale AudioUnit cannot survive reconfiguration.
                    let stopRecord = module.stopRecording()
                    let stopPlay = module.stopPlayout()
                    self.audioLog.info("standalone repair stopRecording=\(stopRecord) stopPlayout=\(stopPlay)")
                    do { try self.configureStandaloneSession() }
                    catch { return false }
                    guard stopRecord == 0, stopPlay == 0 else { return false }
                }
                let recording = module.initAndStartRecording()
                let playoutInit = module.initPlayout()
                let playout = module.startPlayout()
                if self.standaloneAudio { self.applyStandaloneRoute() }
                self.audioLog.info("ADM repair recording=\(recording) initPlayout=\(playoutInit) playout=\(playout)")
                return recording == 0 && playoutInit == 0 && playout == 0
            }, requiresStandaloneSession: standaloneAudio)
        } catch {
            if !Task.isCancelled, !audioRevoked { await reportAudio(module, failed: true) }
            return
        }
        guard !Task.isCancelled, !audioRevoked, !closed else { return }
        let failed = !(standaloneAudio ? result.standaloneAudioReady : result.platformAudioReady)
        await reportAudio(module, failed: failed)
        if failed { microphone?.isEnabled = false }
    }

    func finishRtpSampling() { rtpSampler.finish() }

    private func requestRtpStats(sequence: Int, elapsedMs: Int) {
        guard let peer else { return }
        peer.statistics { [weak self] report in
            let reports = report.statistics.values.map {
                CallRtpReport(id: $0.id, type: $0.type, values: $0.values)
            }
            guard let packet = CallRtpStats.packet(reports: reports, sequence: sequence, elapsedMs: elapsedMs) else { return }
            Task { @MainActor [weak self] in
                guard let self, sequence > latestRtpSequence else { return }
                latestRtpSequence = sequence
                latestRtpStats = packet
                // Diagnostics never wait for the channel or hold media teardown.
                guard !closed else { return }
                await eventHandler?(.rtpStats(packet))
            }
        }
    }

    private func audioSnapshot(_ module: LKRTCAudioDeviceModule) -> CallAudioEngineSnapshot {
        let state = module.engineState
        return CallAudioEngineSnapshot(running: module.isEngineRunning,
            recording: module.recording, playing: module.playing,
            inputEnabled: state.inputEnabled.boolValue, inputRunning: state.inputRunning.boolValue,
            outputEnabled: state.outputEnabled.boolValue, outputRunning: state.outputRunning.boolValue,
            inputMuted: state.inputMuted.boolValue, muteMode: state.muteMode.rawValue)
    }

    private func sessionSnapshot() -> CallAudioEngineSnapshot {
        var snapshot = factory.map { audioSnapshot($0.audioDeviceModule) } ?? CallAudioEngineSnapshot(
            running: false, recording: false, playing: false, inputEnabled: false, inputRunning: false,
            outputEnabled: false, outputRunning: false, inputMuted: false, muteMode: -1)
        let session = AVAudioSession.sharedInstance()
        snapshot.audioSession = CallAudioSessionSnapshot(
            category: session.category.rawValue, mode: session.mode.rawValue,
            inputAvailable: session.isInputAvailable, inputNumberOfChannels: session.inputNumberOfChannels,
            inputs: session.currentRoute.inputs.prefix(8).map { $0.portType.rawValue },
            outputs: session.currentRoute.outputs.prefix(8).map { $0.portType.rawValue })
        return snapshot
    }

    func captureAudioDiagnostic(stage: CallTimingMark) {
        guard !closed, !audioRevoked else { return }
        if audioNotifications.isEmpty {
            for name in [AVAudioSession.routeChangeNotification, AVAudioSession.interruptionNotification,
                         AVAudioSession.mediaServicesWereResetNotification] {
                let token = NotificationCenter.default.addObserver(forName: name, object: nil, queue: nil) { [weak self] note in
                    let detail = "\(note.name.rawValue) monotonic=\(CallMonotonic.now()) reason=\(note.userInfo?[AVAudioSessionRouteChangeReasonKey] ?? "-") type=\(note.userInfo?[AVAudioSessionInterruptionTypeKey] ?? "-") options=\(note.userInfo?[AVAudioSessionInterruptionOptionKey] ?? "-")"
                    Task { @MainActor [weak self] in
                        guard let self, !closed, !audioRevoked else { return }
                        logAudio(detail)
                    }
                }
                audioNotifications.append(token)
            }
        }
        let instant = CallMonotonic.now()
        logAudio("snapshot stage=\(stage.rawValue) monotonic=\(instant)")
        delegate.relay?.yield(.audioDiagnostic(sessionSnapshot(), stage: stage, at: instant))
    }

    private func logAudio(_ stage: String) {
        let session = AVAudioSession.sharedInstance()
        let rtc = LKRTCAudioSession.sharedInstance()
        // Only port types, never device names, UIDs, audio data or SDP.
        audioLog.info("owner=\(self.owner.uuidString, privacy: .public) \(stage, privacy: .public) category=\(session.category.rawValue, privacy: .public) mode=\(session.mode.rawValue, privacy: .public) inputs=\(session.currentRoute.inputs.map { $0.portType.rawValue }.description, privacy: .public) channels=\(session.inputNumberOfChannels) useManualAudio=\(rtc.useManualAudio) isAudioEnabled=\(rtc.isAudioEnabled) isActive=\(rtc.isActive)")
    }

    private func reportAudio(_ module: LKRTCAudioDeviceModule, failed: Bool) async {
        logAudio("verify failed=\(failed)")
        let event = CallMediaTransportEvent.audioEngine(sessionSnapshot(), failed: failed)
        if standaloneAudio {
            // Keep failure behind the captured activation/repair diagnostics.
            delegate.relay?.yield(event)
        } else {
            await eventHandler?(event)
        }
    }

    func publishControl(_ data: Data) async throws {
        try ensureOpen()
        guard data.count <= 4096, let channel, channel.readyState == .open,
              channel.sendData(LKRTCDataBuffer(data: data, isBinary: false)) else { throw DirectMediaError.closed }
    }

    func setSpeakerphone(_ enabled: Bool) {
        speakerPreferred = enabled
        // CallKit owns routing, including Bluetooth; only an app-dialed call routes itself.
        applyStandaloneRoute()
    }

    func disconnect() async {
        guard !closed else { return }
        closed = true
        stopDirectAudio()
        delegate.finish()
        channel?.close()
        peer?.close()
        channel = nil
        peer = nil
        microphone = nil
        factory = nil
        WebRtcAudioBridge.didClose(owner)
        eventHandler = nil
    }

    private func ensureOpen() throws {
        try Task.checkCancellation()
        guard !closed else { throw DirectMediaError.closed }
    }
}

/// CallMediaSession invokes activate only after its CallKit activation callback.
/// An old connection cannot disable a newer transport's shared native session.
@MainActor
enum WebRtcAudioBridge {
    private static let log = Logger(subsystem: "ai.bondings.callpilot.sim", category: "DirectAudio")
    private static let gate = CallDirectAudioGate()
    static func owns(_ id: UUID) -> Bool { gate.owner == id }
    static func preparePrivacyGate(_ id: UUID) throws {
        let pending = gate.lastReleased != nil
        do {
            guard try gate.prepare(id) else { return }
        } catch {
            log.error("direct audio register rejected: owner_present=true")
            throw error
        }
        log.info("direct audio privacy gate prepared: prior_release_pending=\(pending)")
        let session = LKRTCAudioSession.sharedInstance()
        session.useManualAudio = true
        session.isAudioEnabled = false
    }
    static func acquire(_ id: UUID) throws { try preparePrivacyGate(id) }
    static func activate(_ id: UUID) throws {
        try gate.activate(id)
        let session = LKRTCAudioSession.sharedInstance()
        synchronizeRTCConfiguration(session)
        session.audioSessionDidActivate(AVAudioSession.sharedInstance())
        session.isAudioEnabled = true
    }
    private static func synchronizeRTCConfiguration(_ session: LKRTCAudioSession) {
        session.lockForConfiguration()
        defer { session.unlockForConfiguration() }
        let current = AVAudioSession.sharedInstance()
        let previous = LKRTCAudioSessionConfiguration.webRTC()
        // The platformDefault ADM reapplies this cached configuration when its
        // manual gate opens. AVAudioSession.setCategory alone does not update it.
        // Publish a fresh object, never mutate one a native reader may retain.
        let config = LKRTCAudioSessionConfiguration()
        config.category = current.category.rawValue
        config.categoryOptions = current.categoryOptions
        config.mode = current.mode.rawValue
        config.sampleRate = previous.sampleRate
        config.ioBufferDuration = previous.ioBufferDuration
        config.inputNumberOfChannels = previous.inputNumberOfChannels
        config.outputNumberOfChannels = previous.outputNumberOfChannels
        log.info("RTC configuration sync: cached=\(previous.category, privacy: .public)/\(previous.mode, privacy: .public) target=\(config.category, privacy: .public)/\(config.mode, privacy: .public)")
        LKRTCAudioSessionConfiguration.setWebRTC(config)
    }
    static func release(_ id: UUID) {
        guard gate.release(id) else {
            log.debug("direct audio release ignored: newer_owner=\(gate.owner != nil)")
            return
        }
        let session = LKRTCAudioSession.sharedInstance()
        session.isAudioEnabled = false
        session.audioSessionDidDeactivate(AVAudioSession.sharedInstance())
        log.info("direct audio released: peer_close_pending=true")
        // Keep manual gating until this peer has actually closed. Otherwise a
        // late native operation could restart an old default audio device.
    }
    static func didClose(_ id: UUID) {
        guard gate.didClose(id) else { return }
        LKRTCAudioSession.sharedInstance().useManualAudio = false
        log.info("direct audio closed: owner_present=false")
    }
}

private enum DirectMediaError: Error { case closed, unavailable, invalidSDP, audioInactive, timeout }

/// Cancellation resumes the waiter without depending on a WebRTC callback.
private final class DirectSignal: @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<Void, Error>?
    private var waiters: [UUID: CheckedContinuation<Void, Error>] = [:]

    func fire(_ result: Result<Void, Error> = .success(())) {
        lock.lock()
        guard self.result == nil else { lock.unlock(); return }
        self.result = result
        let pending = waiters
        waiters.removeAll()
        lock.unlock()
        for continuation in pending.values { continuation.resume(with: result) }
    }

    func wait() async throws {
        let id = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                lock.lock()
                if let result { lock.unlock(); continuation.resume(with: result) }
                else if Task.isCancelled { lock.unlock(); continuation.resume(throwing: CancellationError()) }
                else { waiters[id] = continuation; lock.unlock() }
            }
        } onCancel: {
            lock.lock()
            let continuation = waiters.removeValue(forKey: id)
            lock.unlock()
            continuation?.resume(throwing: CancellationError())
        }
    }

    func wait(maximum: Duration) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await self.wait() }
            group.addTask { try await Task.sleep(for: maximum); throw DirectMediaError.timeout }
            defer { group.cancelAll() }
            try await group.next()
        }
    }
}

private final class DirectPeerDelegate: NSObject, LKRTCPeerConnectionDelegate, LKRTCDataChannelDelegate, LKRTCAudioRenderer, @unchecked Sendable {
    let gathered = DirectSignal()
    let gatherComplete = DirectSignal()
    let connected = DirectSignal()
    let channelOpen = DirectSignal()
    var relay: CallMediaEventRelay?
    private let rendererLock = NSLock()
    private var rendererRemoval: CallFirstFrameRendererRemoval?
    private weak var remoteAudio: LKRTCAudioTrack?
    private var audioRevoked = false
    private var finished = false

    func revokeAudio() {
        rendererLock.lock()
        defer { rendererLock.unlock() }
        audioRevoked = true
        remoteAudio?.isEnabled = false
    }

    func finish() {
        rendererLock.lock()
        finished = true
        let removal = rendererRemoval
        rendererLock.unlock()
        removal?.remove()
        gathered.fire(.failure(DirectMediaError.closed))
        gatherComplete.fire(.failure(DirectMediaError.closed))
        connected.fire(.failure(DirectMediaError.closed))
        channelOpen.fire(.failure(DirectMediaError.closed))
        relay?.finish()
    }
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didChange stateChanged: LKRTCSignalingState) {}
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didAdd stream: LKRTCMediaStream) {}
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didRemove stream: LKRTCMediaStream) {}
    func peerConnectionShouldNegotiate(_ peerConnection: LKRTCPeerConnection) {}
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didChange newState: LKRTCIceConnectionState) {}
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didRemove candidates: [LKRTCIceCandidate]) {}
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didOpen dataChannel: LKRTCDataChannel) {}
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didAdd receiver: LKRTCRtpReceiver,
                        streams: [LKRTCMediaStream]) {
        guard let track = receiver.track as? LKRTCAudioTrack else { return }
        rendererLock.lock()
        defer { rendererLock.unlock() }
        if audioRevoked { track.isEnabled = false }
        guard !finished, rendererRemoval == nil else { return }
        remoteAudio = track
        rendererRemoval = CallFirstFrameRendererRemoval { [weak self] in
            guard let self else { return }
            track.remove(self)
        }
        relay?.yield(.timing(.downlinkSubscribed, at: CallMonotonic.now()))
        track.add(self)
    }
    func render(pcmBuffer: AVAudioPCMBuffer) {
        guard pcmBuffer.frameLength > 0 else { return }
        rendererLock.lock()
        defer { rendererLock.unlock() }
        guard !finished else { return }
        rendererRemoval?.recordFirstFrame {
            relay?.yield(.timing(.downlinkFirstFrame, at: CallMonotonic.now()))
        }
    }
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didGenerate candidate: LKRTCIceCandidate) {
        if candidate.sdp.contains(" typ relay") { gathered.fire() }
    }
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didChange newState: LKRTCIceGatheringState) {
        if newState == .complete { gatherComplete.fire(); gathered.fire() }
    }
    func peerConnection(_ peerConnection: LKRTCPeerConnection, didChange newState: LKRTCPeerConnectionState) {
        switch newState {
        case .connected: connected.fire()
        case .failed, .closed:
            connected.fire(.failure(DirectMediaError.closed))
            channelOpen.fire(.failure(DirectMediaError.closed))
            relay?.yield(.disconnected(reason: "direct_transport_failed"))
        default: break
        }
    }
    func dataChannelDidChangeState(_ dataChannel: LKRTCDataChannel) {
        if dataChannel.readyState == .open {
            relay?.yield(.timing(.directControlReady, at: CallMonotonic.now()))
            channelOpen.fire()
        }
        if dataChannel.readyState == .closed {
            channelOpen.fire(.failure(DirectMediaError.closed))
            relay?.yield(.disconnected(reason: "direct_control_closed"))
        }
    }
    func dataChannel(_ dataChannel: LKRTCDataChannel, didReceiveMessageWith buffer: LKRTCDataBuffer) {
        guard buffer.data.count <= 4096 else { return }
        relay?.yield(.data(buffer.data, topic: CallPilotTopic.status))
    }
}
