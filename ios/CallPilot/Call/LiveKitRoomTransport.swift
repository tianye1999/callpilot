import Foundation
import LiveKit

@MainActor
final class LiveKitRoomTransport: CallMediaTransport {
    static func connectOptions(enableMicrophone: Bool) -> ConnectOptions {
        ConnectOptions(enableMicrophone: enableMicrophone)
    }
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
        room = Room(delegate: proxy)
    }

    func connect(url: String, token: String, enableMicrophone: Bool) async throws {
        try await room.connect(
            url: url,
            token: token,
            connectOptions: Self.connectOptions(enableMicrophone: enableMicrophone)
        )
    }

    func setMicrophone(enabled: Bool) async throws {
        try Task.checkCancellation()
        try await room.localParticipant.setMicrophone(enabled: enabled)
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
