import Foundation
import LiveKit
import AVFAudio

@MainActor
final class LiveKitRoomTransport: CallMediaTransport {
    static func connectOptions(enableMicrophone: Bool) -> ConnectOptions {
        ConnectOptions(enableMicrophone: enableMicrophone)
    }
    static let silentConnectOptions = ConnectOptions(autoSubscribe: false, enableMicrophone: false)
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

    func connectSilent(url: String, token: String) async throws {
        try await room.connect(url: url, token: token, connectOptions: Self.silentConnectOptions)
        await eventTarget.emit(.permissionsReady(Self.mediaAllowed(room.localParticipant.permissions)))
    }

    func subscribeToEdge(_ identity: String) async throws {
        for participant in room.remoteParticipants.values where participant.identity?.stringValue == identity {
            for publication in participant.trackPublications.values {
                guard let remote = publication as? RemoteTrackPublication,
                      remote.name == "phone-downlink", remote.kind == .audio else { continue }
                try Task.checkCancellation()
                try await remote.set(subscribed: true)
            }
        }
    }

    nonisolated static func mediaAllowed(_ permission: ParticipantPermissions) -> Bool {
        permission.canPublish && permission.canSubscribe && permission.canPublishData
            && permission.canPublishSources == [Track.Source.microphone.rawValue]
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

private final class LiveKitRoomDelegateProxy: NSObject, RoomDelegate, AudioRenderer, @unchecked Sendable {
    private let relay: CallMediaEventRelay
    private let lock = NSLock()
    private var rendererRemoval: CallFirstFrameRendererRemoval?
    private var finished = false

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

    func room(_ room: Room, participant: Participant, didUpdatePermissions permissions: ParticipantPermissions) {
        guard participant is LocalParticipant else { return }
        relay.yield(.permissionsReady(LiveKitRoomTransport.mediaAllowed(permissions)))
    }

    func room(_ room: Room, participant: RemoteParticipant, didPublishTrack publication: RemoteTrackPublication) {
        relay.yield(.tracksChanged)
    }

    func room(_ room: Room, participant: RemoteParticipant, didSubscribeTrack publication: RemoteTrackPublication) {
        guard let track = publication.track as? RemoteAudioTrack, track.name == "phone-downlink" else { return }
        lock.lock()
        defer { lock.unlock() }
        guard !finished, rendererRemoval == nil else { return }
        rendererRemoval = CallFirstFrameRendererRemoval { [weak self] in
            guard let self else { return }
            track.remove(audioRenderer: self)
        }
        relay.yield(.timing(.downlinkSubscribed, at: CallMonotonic.now()))
        track.add(audioRenderer: self)
    }

    func render(pcmBuffer: AVAudioPCMBuffer) {
        guard pcmBuffer.frameLength > 0 else { return }
        lock.lock()
        defer { lock.unlock() }
        guard !finished else { return }
        rendererRemoval?.recordFirstFrame {
            relay.yield(.timing(.downlinkFirstFrame, at: CallMonotonic.now()))
        }
    }

    func finish() {
        lock.lock()
        finished = true
        let removal = rendererRemoval
        rendererRemoval = nil
        lock.unlock()
        removal?.remove()
        relay.finish()
    }
}
