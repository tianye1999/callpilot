@preconcurrency import AVFAudio
@preconcurrency import CallKit
import Foundation
@preconcurrency import PushKit

@MainActor
protocol CallKitCoordinatorDelegate: AnyObject {
    var callKitCanAcceptIncomingCall: Bool { get }
    func callKitDidUpdateToken(_ token: String, environment: ApnsEnvironment)
    func callKitDidInvalidateToken()
    func callKitDidReceiveOffer(_ offer: InboundOffer)
    func callKitDidRequestAnswer(_ offer: InboundOffer)
    func callKitDidRequestDecline(_ offer: InboundOffer)
    func callKitDidRequestHangup(offerId: String)
    func callKitPrepareToAnswer(offerId: String) throws
    func callKitDidActivateAudio(offerId: String) throws
    func callKitDidFail(offerId: String, code: String)
    func callKitAudioStopper(offerId: String) -> () -> Void
}

@MainActor
final class CallKitCoordinator: NSObject {
    weak var delegate: (any CallKitCoordinatorDelegate)?
    private(set) var currentToken: (value: String, environment: ApnsEnvironment)?

    private let provider: CXProvider
    private let controller: CXCallController
    private var pushRegistry: PKPushRegistry?
    private var calls = CallKitCallRegistry()
    private var expirationTasks: [UUID: Task<Void, Never>] = [:]
    private lazy var answers = CallKitAnswerController(
        configureAudio: { [weak self] id in
            guard let self, let payload = calls.payload(callUUID: id), let delegate else {
                throw CancellationError()
            }
            try delegate.callKitPrepareToAnswer(offerId: payload.offerId)
        },
        activateAudio: { [weak self] id in
            guard let self, let payload = calls.payload(callUUID: id), let delegate else {
                throw CancellationError()
            }
            try delegate.callKitDidActivateAudio(offerId: payload.offerId)
        },
        onFailure: { [weak self] id, code in self?.failAnswer(callUUID: id, code: code) },
        onDeactivate: { [weak self] id in
            self?.failAnswer(callUUID: id, code: "CALLKIT_AUDIO_DEACTIVATED")
        }
    )

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

    var isAudioSessionDrained: Bool { !answers.hasUnresolvedAudioEvents }

    func waitUntilAudioDrained(offerId: String?) async -> Bool {
        if let offerId {
            guard let id = calls.callUUID(offerId: offerId), isAnswering(offerId: offerId) else { return false }
            return await answers.waitUntilAudioDrained(answerID: id)
        }
        return await answers.waitUntilAudioDrained()
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
        let phase = calls.phase(callUUID: callUUID)
        if phase == .active || phase == .answering {
            delegate?.callKitDidRequestHangup(offerId: offer.offerId)
        }
        do {
            try await request(CXEndCallAction(call: callUUID))
        } catch {
            delegate?.callKitDidRequestHangup(offerId: offer.offerId)
            finish(callUUID: callUUID, reason: .failed)
        }
        return true
    }

    func requestEndActiveIfManaged() -> Bool {
        guard let callUUID = pendingOrActiveCallUUID(),
              let payload = calls.payload(callUUID: callUUID) else { return false }
        // Capture this call and register intent before the UI can admit a replacement.
        delegate?.callKitDidRequestHangup(offerId: payload.offerId)
        Task {
            do {
                try await request(CXEndCallAction(call: callUUID))
            } catch {
                delegate?.callKitDidRequestHangup(offerId: payload.offerId)
                finish(callUUID: callUUID, reason: .failed)
            }
        }
        return true
    }

    func markConnected(offerId: String) {
        guard let callUUID = calls.callUUID(offerId: offerId) else { return }
        _ = calls.markConnected(callUUID: callUUID)
    }

    func isAnswering(offerId: String) -> Bool {
        guard let id = calls.callUUID(offerId: offerId) else { return false }
        return calls.phase(callUUID: id) == .answering
    }

    func markClaimed(offerId: String) throws {
        guard let callUUID = calls.callUUID(offerId: offerId),
              calls.phase(callUUID: callUUID) == .answering else { throw CancellationError() }
        let stopAudio = delegate?.callKitAudioStopper(offerId: offerId) ?? {}
        try answers.claimSucceeded(callID: callUUID, stopAudio: stopAudio)
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
    }

    private func report(_ payload: VoipPushPayload, completion: PushCompletion) {
        let now = Int64(Date().timeIntervalSince1970 * 1_000)
        guard calls.register(payload, nowUnixMs: now) else {
            completion.call()
            return
        }
        let update = CXCallUpdate()
        update.localizedCallerName = L10n.text("callkit.incoming.name")
        update.remoteHandle = CXHandle(type: .generic, value: "KinyAI SIM")
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
        if answers.end(callID: callUUID) {
            provider.reportCall(with: callUUID, endedAt: Date(), reason: reason)
        }
        if phase == .ringing {
            delegate?.callKitDidRequestDecline(InboundOffer(
                offerId: payload.offerId,
                callUUID: payload.callUUID,
                expiresAt: payload.expiresAtUnixMs
            ))
        }
    }

    private func pendingOrActiveCallUUID() -> UUID? {
        calls.firstCallUUID(in: .active)
            ?? calls.firstCallUUID(in: .answering)
    }

    private func failAnswer(callUUID: UUID, code: String) {
        guard let payload = calls.payload(callUUID: callUUID) else { return }
        finish(callUUID: callUUID, reason: .failed)
        delegate?.callKitDidFail(offerId: payload.offerId, code: code)
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
            let environment = PushEnvironment.current()
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
        // The registry is created on the main queue. Report to CallKit before
        // returning from this callback; an asynchronous actor hop makes iOS
        // classify the VoIP push as unhandled and terminate the app.
        MainActor.assumeIsolated { [weak self] in
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
        MainActor.assumeIsolated {
            self.expirationTasks.values.forEach { $0.cancel() }
            self.expirationTasks.removeAll()
            let removed = self.calls.removeAll()
            for payload in removed {
                self.delegate?.callKitDidRequestHangup(offerId: payload.offerId)
            }
            self.answers.reset()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        MainActor.assumeIsolated {
            guard let payload = self.calls.beginAnswer(callUUID: action.callUUID) else {
                if !action.isComplete, Date() < action.timeoutDate { action.fail() }
                return
            }
            self.expirationTasks.removeValue(forKey: action.callUUID)?.cancel()
            self.answers.begin(callID: action.callUUID, action: CallKitAnswerActionAdapter(action))
            self.delegate?.callKitDidRequestAnswer(InboundOffer(
                offerId: payload.offerId,
                callUUID: payload.callUUID,
                expiresAt: payload.expiresAtUnixMs
            ))
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        MainActor.assumeIsolated {
            let phase = self.calls.phase(callUUID: action.callUUID)
            guard let payload = self.calls.remove(callUUID: action.callUUID) else {
                if !action.isComplete, Date() < action.timeoutDate { action.fail() }
                return
            }
            self.expirationTasks.removeValue(forKey: action.callUUID)?.cancel()
            if phase == .active || phase == .answering {
                self.delegate?.callKitDidRequestHangup(offerId: payload.offerId)
            } else {
                self.delegate?.callKitDidRequestDecline(InboundOffer(
                    offerId: payload.offerId,
                    callUUID: payload.callUUID,
                    expiresAt: payload.expiresAtUnixMs
                ))
            }
            _ = self.answers.end(callID: action.callUUID)
            if !action.isComplete, Date() < action.timeoutDate { action.fulfill() }
        }
    }

    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        // Provider callbacks use .main. Consume the epoch synchronously rather
        // than looking up whichever call happens to be current in a later Task.
        MainActor.assumeIsolated { answers.didActivate() }
    }

    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        MainActor.assumeIsolated { answers.didDeactivate() }
    }

    nonisolated func provider(_ provider: CXProvider, timedOutPerforming action: CXAction) {
        MainActor.assumeIsolated {
            if let answer = action as? CXAnswerCallAction {
                answers.timedOut(callID: answer.callUUID)
            } else if let callAction = action as? CXCallAction {
                failAnswer(callUUID: callAction.callUUID, code: "CALLKIT_ACTION_TIMEOUT")
            }
        }
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

@MainActor
private final class CallKitAnswerActionAdapter: CallKitAnswerAction {
    private let action: CXAnswerCallAction
    init(_ action: CXAnswerCallAction) { self.action = action }
    var deadline: Date { action.timeoutDate }
    var isComplete: Bool { action.isComplete }
    func fulfill() { action.fulfill() }
    func fail() { action.fail() }
}
