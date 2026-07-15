import Foundation
import LiveKit

enum CallMediaTransportEvent: Equatable, Sendable {
    case data(Data, topic: String)
    case disconnected(reason: String)
}

@MainActor
protocol CallMediaTransport: AnyObject {
    var eventHandler: ((CallMediaTransportEvent) async -> Void)? { get set }

    func connect(url: String, token: String) async throws
    func publishControl(_ data: Data) async throws
    func disconnect() async
    func setSpeakerphone(_ enabled: Bool)
}

@MainActor
final class LiveKitRoomTransport: CallMediaTransport {
    static let connectOptions = ConnectOptions(enableMicrophone: true)
    static let controlPublishOptions = DataPublishOptions(
        topic: CallPilotTopic.control,
        reliable: true
    )

    private let eventTarget: LiveKitRoomEventTarget
    private let delegateProxy: LiveKitRoomDelegateProxy
    private let room: Room

    var eventHandler: ((CallMediaTransportEvent) async -> Void)? {
        get { eventTarget.handler }
        set { eventTarget.handler = newValue }
    }

    init() {
        let target = LiveKitRoomEventTarget()
        let proxy = LiveKitRoomDelegateProxy(target: target)
        eventTarget = target
        delegateProxy = proxy
        room = Room(delegate: proxy, connectOptions: Self.connectOptions)
    }

    func connect(url: String, token: String) async throws {
        try await room.connect(
            url: url,
            token: token,
            connectOptions: Self.connectOptions
        )
    }

    func publishControl(_ data: Data) async throws {
        try Task.checkCancellation()
        try await room.localParticipant.publish(
            data: data,
            options: Self.controlPublishOptions
        )
    }

    func disconnect() async {
        delegateProxy.finish()
        await room.disconnect()
    }

    func setSpeakerphone(_ enabled: Bool) {
        AudioManager.shared.isSpeakerOutputPreferred = enabled
    }
}

@MainActor
private final class LiveKitRoomEventTarget {
    var handler: ((CallMediaTransportEvent) async -> Void)?

    func emit(_ event: CallMediaTransportEvent) async {
        await handler?(event)
    }
}

/// Preserves the SDK delegate's FIFO event order while crossing into
/// MainActor-isolated call state. A Task per callback would not provide that
/// ordering guarantee once one delivery suspends.
final class CallMediaEventRelay: Sendable {
    private let continuation: AsyncStream<CallMediaTransportEvent>.Continuation
    private let consumer: Task<Void, Never>

    init(
        deliver: @escaping @MainActor (CallMediaTransportEvent) async -> Void
    ) {
        let (stream, continuation) = AsyncStream.makeStream(
            of: CallMediaTransportEvent.self,
            bufferingPolicy: .unbounded
        )
        self.continuation = continuation
        consumer = Task { @MainActor in
            for await event in stream {
                guard !Task.isCancelled else { break }
                await deliver(event)
            }
        }
    }

    deinit {
        finish()
    }

    func yield(_ event: CallMediaTransportEvent) {
        continuation.yield(event)
    }

    func finish() {
        continuation.finish()
        consumer.cancel()
    }
}

private final class LiveKitRoomDelegateProxy: NSObject, RoomDelegate, @unchecked Sendable {
    private let relay: CallMediaEventRelay

    init(target: LiveKitRoomEventTarget) {
        relay = CallMediaEventRelay { [weak target] event in
            await target?.emit(event)
        }
        super.init()
    }

    func room(
        _ room: Room,
        participant: RemoteParticipant?,
        didReceiveData data: Data,
        forTopic topic: String,
        encryptionType: EncryptionType
    ) {
        relay.yield(.data(data, topic: topic))
    }

    func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        let reason = error?.localizedDescription ?? "media_disconnected"
        relay.yield(.disconnected(reason: reason))
    }

    func finish() {
        relay.finish()
    }
}
