@preconcurrency import AVFAudio
@preconcurrency import CallKit
import Foundation
import LiveKit
@preconcurrency import PushKit

@MainActor
protocol CallKitCoordinatorDelegate: AnyObject {
    var callKitCanAcceptIncomingCall: Bool { get }
    func callKitDidUpdateToken(_ token: String, environment: ApnsEnvironment)
    func callKitDidInvalidateToken()
    func callKitDidReceiveOffer(_ offer: InboundOffer)
    func callKitDidRequestAnswer(_ offer: InboundOffer)
    func callKitDidRequestDecline(_ offer: InboundOffer)
    func callKitDidRequestHangup()
}

@MainActor
final class CallKitCoordinator: NSObject {
    weak var delegate: (any CallKitCoordinatorDelegate)?
    private(set) var currentToken: (value: String, environment: ApnsEnvironment)?

    private let provider: CXProvider
    private let controller: CXCallController
    private var pushRegistry: PKPushRegistry?
    private var calls = CallKitCallRegistry()
    private var pendingAnswers: [UUID: CXAnswerCallAction] = [:]
    private var expirationTasks: [UUID: Task<Void, Never>] = [:]

    override init() {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]
        configuration.includesCallsInRecents = false
        provider = CXProvider(configuration: configuration)
        controller = CXCallController()
        super.init()
        provider.setDelegate(self, queue: .main)
    }

    func start() {
        guard pushRegistry == nil else { return }
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        pushRegistry = registry
    }

    func requestAnswerIfManaged(_ offer: InboundOffer) async -> Bool {
        guard let callUUID = calls.callUUID(offerId: offer.offerId),
              calls.phase(callUUID: callUUID) == .ringing else { return false }
        do {
            try await request(CXAnswerCallAction(call: callUUID))
        } catch {
            finish(callUUID: callUUID, reason: .failed)
        }
        return true
    }

    func requestEndIfManaged(_ offer: InboundOffer) async -> Bool {
        guard let callUUID = calls.callUUID(offerId: offer.offerId) else { return false }
        do {
            try await request(CXEndCallAction(call: callUUID))
        } catch {
            finish(callUUID: callUUID, reason: .failed)
        }
        return true
    }

    func requestEndActiveIfManaged() async -> Bool {
        guard let callUUID = pendingOrActiveCallUUID() else { return false }
        do {
            try await request(CXEndCallAction(call: callUUID))
        } catch {
            finish(callUUID: callUUID, reason: .failed)
        }
        return true
    }

    func markConnected(offerId: String) {
        guard let callUUID = calls.callUUID(offerId: offerId),
              calls.markConnected(callUUID: callUUID) else { return }
        pendingAnswers.removeValue(forKey: callUUID)?.fulfill()
    }

    func markMediaJoined(offerId: String) {
        guard let callUUID = calls.callUUID(offerId: offerId),
              calls.phase(callUUID: callUUID) == .answering else { return }
        pendingAnswers.removeValue(forKey: callUUID)?.fulfill()
    }

    func finishActiveCall(reason: CXCallEndedReason) {
        guard let callUUID = pendingOrActiveCallUUID() else { return }
        finish(callUUID: callUUID, reason: reason)
    }

    func reconcile(openOffers: [InboundOffer], nowUnixMs: Int64) {
        let stale = calls.reconcile(
            openOfferIds: Set(openOffers.map(\.offerId)),
            nowUnixMs: nowUnixMs
        )
        for callUUID in stale {
            expirationTasks.removeValue(forKey: callUUID)?.cancel()
            provider.reportCall(with: callUUID, endedAt: Date(), reason: .unanswered)
        }
        if !stale.isEmpty { CallKitAudioSessionBridge.prepareForStandaloneCall() }
    }

    private func report(_ payload: VoipPushPayload, completion: PushCompletion) {
        let now = Int64(Date().timeIntervalSince1970 * 1_000)
        guard calls.register(payload, nowUnixMs: now) else {
            completion.call()
            return
        }
        let preparedAudio = delegate?.callKitCanAcceptIncomingCall == true
        if preparedAudio { CallKitAudioSessionBridge.prepareForIncoming() }
        let update = CXCallUpdate()
        update.localizedCallerName = L10n.text("callkit.incoming.name")
        update.remoteHandle = CXHandle(type: .generic, value: "CallPilot")
        update.hasVideo = false
        update.supportsDTMF = true
        update.supportsGrouping = false
        update.supportsHolding = false
        update.supportsUngrouping = false
        provider.reportNewIncomingCall(with: payload.callUUID, update: update) { [weak self] error in
            Task { @MainActor in
                guard let self else {
                    completion.call()
                    return
                }
                if error == nil {
                    let offer = InboundOffer(
                        offerId: payload.offerId,
                        callUUID: payload.callUUID,
                        expiresAt: payload.expiresAtUnixMs
                    )
                    self.delegate?.callKitDidReceiveOffer(offer)
                    self.scheduleExpiration(payload)
                } else {
                    _ = self.calls.remove(callUUID: payload.callUUID)
                    if preparedAudio { CallKitAudioSessionBridge.prepareForStandaloneCall() }
                }
                completion.call()
            }
        }
    }

    private func scheduleExpiration(_ payload: VoipPushPayload) {
        expirationTasks[payload.callUUID]?.cancel()
        expirationTasks[payload.callUUID] = Task { @MainActor [weak self] in
            let delay = max(
                0,
                payload.expiresAtUnixMs - Int64(Date().timeIntervalSince1970 * 1_000)
            )
            do {
                try await Task.sleep(for: .milliseconds(delay))
            } catch {
                return
            }
            guard let self,
                  self.calls.phase(callUUID: payload.callUUID) == .ringing else { return }
            self.finish(callUUID: payload.callUUID, reason: .unanswered)
        }
    }

    private func finish(callUUID: UUID, reason: CXCallEndedReason) {
        let phase = calls.phase(callUUID: callUUID)
        guard let payload = calls.remove(callUUID: callUUID) else { return }
        expirationTasks.removeValue(forKey: callUUID)?.cancel()
        pendingAnswers.removeValue(forKey: callUUID)?.fail()
        provider.reportCall(with: callUUID, endedAt: Date(), reason: reason)
        if phase == .ringing {
            CallKitAudioSessionBridge.prepareForStandaloneCall()
            delegate?.callKitDidRequestDecline(InboundOffer(
                offerId: payload.offerId,
                callUUID: payload.callUUID,
                expiresAt: payload.expiresAtUnixMs
            ))
        }
    }

    private func pendingOrActiveCallUUID() -> UUID? {
        pendingAnswers.keys.first
            ?? calls.firstCallUUID(in: .active)
            ?? calls.firstCallUUID(in: .answering)
    }

    private func request(_ action: CXCallAction) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            controller.request(CXTransaction(action: action)) { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }
}

extension CallKitCoordinator: PKPushRegistryDelegate {
    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didUpdate pushCredentials: PKPushCredentials,
        for type: PKPushType
    ) {
        guard type == .voIP else { return }
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor [weak self] in
            guard let self else { return }
            #if DEBUG
            let environment = ApnsEnvironment.sandbox
            #else
            let environment = ApnsEnvironment.production
            #endif
            self.currentToken = (token, environment)
            self.delegate?.callKitDidUpdateToken(token, environment: environment)
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didInvalidatePushTokenFor type: PKPushType
    ) {
        guard type == .voIP else { return }
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.currentToken = nil
            self.delegate?.callKitDidInvalidateToken()
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didReceiveIncomingPushWith payload: PKPushPayload,
        for type: PKPushType,
        completion: @escaping () -> Void
    ) {
        guard type == .voIP,
              let decoded = VoipPushPayload.decode(payload.dictionaryPayload) else {
            completion()
            return
        }
        let completionBox = PushCompletion(completion)
        Task { @MainActor [weak self] in
            guard let self else {
                completionBox.call()
                return
            }
            self.report(decoded, completion: completionBox)
        }
    }
}

extension CallKitCoordinator: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            self.pendingAnswers.values.forEach { $0.fail() }
            self.pendingAnswers.removeAll()
            self.expirationTasks.values.forEach { $0.cancel() }
            self.expirationTasks.removeAll()
            _ = self.calls.removeAll()
            CallKitAudioSessionBridge.prepareForStandaloneCall()
            self.delegate?.callKitDidRequestHangup()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor [weak self] in
            guard let self,
                  let payload = self.calls.beginAnswer(callUUID: action.callUUID) else {
                action.fail()
                return
            }
            self.expirationTasks.removeValue(forKey: action.callUUID)?.cancel()
            self.pendingAnswers[action.callUUID] = action
            self.delegate?.callKitDidRequestAnswer(InboundOffer(
                offerId: payload.offerId,
                callUUID: payload.callUUID,
                expiresAt: payload.expiresAtUnixMs
            ))
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        Task { @MainActor [weak self] in
            guard let self else {
                action.fail()
                return
            }
            let phase = self.calls.phase(callUUID: action.callUUID)
            guard let payload = self.calls.remove(callUUID: action.callUUID) else {
                action.fail()
                return
            }
            self.expirationTasks.removeValue(forKey: action.callUUID)?.cancel()
            self.pendingAnswers.removeValue(forKey: action.callUUID)?.fail()
            if phase == .active || phase == .answering {
                self.delegate?.callKitDidRequestHangup()
            } else {
                CallKitAudioSessionBridge.prepareForStandaloneCall()
                self.delegate?.callKitDidRequestDecline(InboundOffer(
                    offerId: payload.offerId,
                    callUUID: payload.callUUID,
                    expiresAt: payload.expiresAtUnixMs
                ))
            }
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        CallKitAudioSessionBridge.didActivate(audioSession)
    }

    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        CallKitAudioSessionBridge.didDeactivate()
    }
}

private final class PushCompletion: @unchecked Sendable {
    private let completion: () -> Void

    init(_ completion: @escaping () -> Void) {
        self.completion = completion
    }

    func call() {
        completion()
    }
}

enum CallKitAudioSessionBridge {
    static func prepareForIncoming() {
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        try? AudioManager.shared.setEngineAvailability(.none)
    }

    static func didActivate(_ session: AVAudioSession) {
        do {
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: [.mixWithOthers])
            try AudioManager.shared.setEngineAvailability(.default)
        } catch {
            try? AudioManager.shared.setEngineAvailability(.none)
        }
    }

    static func didDeactivate() {
        try? AudioManager.shared.setEngineAvailability(.none)
    }

    static func prepareForStandaloneCall() {
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = true
        try? AudioManager.shared.setEngineAvailability(.default)
    }
}
