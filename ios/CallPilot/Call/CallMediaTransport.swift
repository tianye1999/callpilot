import Foundation

enum CallMediaTransportEvent: Equatable, Sendable {
    case data(Data, topic: String)
    case disconnected(reason: String)
    case timing(CallTimingMark, at: TimeInterval)
}

@MainActor
protocol CallMediaTransport: AnyObject {
    var eventHandler: ((CallMediaTransportEvent) async -> Void)? { get set }
    func connect(url: String, token: String, enableMicrophone: Bool) async throws
    func setMicrophone(enabled: Bool) async throws
    func publishControl(_ data: Data) async throws
    func disconnect() async
    func setSpeakerphone(_ enabled: Bool)
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
