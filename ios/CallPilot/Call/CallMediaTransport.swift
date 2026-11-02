import Foundation

enum CallMediaTransportEvent: Equatable, Sendable {
    case data(Data, topic: String)
    case disconnected(reason: String)
    case timing(CallTimingMark, at: TimeInterval)
    case permissionsReady(Bool)
    case tracksChanged
    case audioEngine(CallAudioEngineSnapshot, failed: Bool)
    case rtpStats(Data)
}

@MainActor
final class CallDirectAudioGate {
    private(set) var owner: UUID?
    private(set) var lastReleased: UUID?
    private(set) var active = false

    // Returns true only when the native manual gate needs initialization.
    func prepare(_ id: UUID) throws -> Bool {
        guard owner == nil || owner == id else { throw CallAudioError.busy }
        guard owner != id else { return false }
        owner = id
        lastReleased = nil
        active = false
        return true
    }

    func activate(_ id: UUID) throws {
        guard owner == id else { throw CallAudioError.busy }
        active = true
    }

    func release(_ id: UUID) -> Bool {
        guard owner == id else { return false }
        owner = nil
        lastReleased = id
        active = false
        return true
    }

    func didClose(_ id: UUID) -> Bool {
        guard owner == nil, lastReleased == id else { return false }
        lastReleased = nil
        return true
    }
}

struct CallAudioEngineSnapshot: Codable, Equatable, Sendable {
    let running: Bool
    let recording: Bool
    let playing: Bool
    let inputEnabled: Bool
    let inputRunning: Bool
    let outputEnabled: Bool
    let outputRunning: Bool
    let inputMuted: Bool
    let muteMode: Int

    // platformDefault uses a VoIP AudioUnit, not AVAudioEngine. Its valid
    // readiness evidence is recording/playing; engineState is diagnostic only.
    var platformAudioReady: Bool { recording && playing }
}

@MainActor
enum CallDirectAudioStartup {
    static let maximum: Duration = .milliseconds(200)

    static func verify(snapshot: () -> CallAudioEngineSnapshot,
                       repair: () -> Bool,
                       wait: (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) async throws -> CallAudioEngineSnapshot {
        if snapshot().platformAudioReady { return snapshot() }
        guard repair() else { throw CallAudioError.answerExpired }
        if !snapshot().platformAudioReady { try await wait(maximum) }
        try Task.checkCancellation()
        return snapshot()
    }
}

@MainActor
protocol CallMediaTransport: AnyObject {
    var eventHandler: ((CallMediaTransportEvent) async -> Void)? { get set }
    func connect(url: String, token: String, enableMicrophone: Bool) async throws
    func connectSilent(url: String, token: String) async throws
    func subscribeToEdge(_ identity: String) async throws
    func setMicrophone(enabled: Bool) async throws
    func publishControl(_ data: Data) async throws
    func disconnect() async
    func setSpeakerphone(_ enabled: Bool)
}

extension CallMediaTransport {
    func connectSilent(url: String, token: String) async throws { throw CallAudioError.busy }
    func subscribeToEdge(_ identity: String) async throws { throw CallAudioError.busy }
}

@MainActor
protocol CallDirectMediaTransport: CallMediaTransport {
    func prepareDirect(servers: [CallIceServer]) async throws -> String
    func connectDirect(answer: String) async throws
    func activateDirectAudio() throws
    // Synchronous revocation fences audio before an uncooperative SDK teardown.
    func stopDirectAudio()
    var latestRtpStats: Data? { get }
    func finishRtpSampling()
}

extension CallDirectMediaTransport {
    var latestRtpStats: Data? { nil }
    func finishRtpSampling() {}
}

final class CallFirstFrameRendererRemoval: @unchecked Sendable {
    private let lock = NSLock()
    private var removal: (@Sendable () -> Void)?
    private var receivedFrame = false

    init(removal: @escaping @Sendable () -> Void) {
        self.removal = removal
    }

    func recordFirstFrame(_ record: () -> Void) {
        lock.lock()
        guard removal != nil, !receivedFrame else {
            lock.unlock()
            return
        }
        receivedFrame = true
        record()
        lock.unlock()
        // SDK removal can take the renderer callback lock; leave that thread first.
        DispatchQueue.main.async { self.remove() }
    }

    func remove() {
        lock.lock()
        let removal = removal
        self.removal = nil
        lock.unlock()
        removal?()
    }
}

/// Keeps SDK callbacks FIFO while crossing to the call's main-actor state.
final class CallMediaEventRelay: Sendable {
    private let continuation: AsyncStream<CallMediaTransportEvent>.Continuation
    private let consumer: Task<Void, Never>

    init(deliver: @escaping @MainActor (CallMediaTransportEvent) async -> Void) {
        let (stream, continuation) = AsyncStream.makeStream(
            of: CallMediaTransportEvent.self, bufferingPolicy: .unbounded
        )
        self.continuation = continuation
        consumer = Task { @MainActor in
            for await event in stream {
                guard !Task.isCancelled else { break }
                await deliver(event)
            }
        }
    }

    deinit { finish() }
    func yield(_ event: CallMediaTransportEvent) { continuation.yield(event) }
    func finish() {
        continuation.finish()
        consumer.cancel()
    }
}
