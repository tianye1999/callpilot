import Foundation

enum CallMediaTransportEvent: Equatable, Sendable {
    case data(Data, topic: String)
    case disconnected(reason: String)
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
