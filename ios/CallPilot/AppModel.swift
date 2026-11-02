import Foundation
import Combine

/// App 状态中枢:配对、线路状态轮询、来电 offer 轮询、通话状态机。
/// 对齐 Android CallManager + MainActivity 的组合职责。
/// 媒体会话(LiveKit)由 CallMediaSession 承担;本类只驱动状态与协议。
@MainActor
final class AppModel: ObservableObject {
    @Published var pairing: StoredPairing?
    @Published var selectedTab: MainTab = .dial
    @Published private(set) var alertRouteID: UUID?
    @Published var callState: CallState = .idle
    @Published var incomingOffer: InboundOffer?
    @Published var lineStatusLabel = L10n.text("line.status.checking")
    @Published var pairingError: String?
    @Published var lineReady = false
    @Published private(set) var speakerphoneEnabled = false
    @Published private(set) var messageInbox: MessageInboxModel?
    @Published private(set) var callHistory: CallHistoryModel?
    @Published private(set) var deviceStatus: HostedDeviceStatus?
    @Published private(set) var deviceStatusSync: DeviceStatusSyncState = .idle
    @Published private(set) var deviceStatusRefreshing = false

    private let store = CredentialStore()
    private var client: HostedCloudClient?
    private var dismissedOffers = Set<String>()
    private var media: CallMediaSession?
    private var mediaTimeoutTask: Task<Void, Never>?
    private var callResultTask: Task<Void, Never>?
    private let endedResultDisplayDuration: Duration = .seconds(5)
    private var callKitOfferId: String?
    private var pendingAudioAttempt: (attempt: CallAttempt, label: String)?
    private var callAttempts = CallAttemptStateMachine()
    private let messageStore = FileMessageCacheStore()
    private let callHistoryStore = FileCallHistoryCacheStore()
    private var deviceStatusMachine = DeviceStatusStateMachine()
    private let callKit = CallKitCoordinator()
    private let claimPrewarmer = ClaimPrewarmer()
    private var silentIncoming: (offer: InboundOffer, session: CallMediaSession)?
    private var silentIncomingTask: Task<Void, Never>?
    private var silentExpiryTask: Task<Void, Never>?

    private func closeSilentIncoming() {
        silentIncomingTask?.cancel()
        silentIncomingTask = nil
        silentExpiryTask?.cancel()
        silentExpiryTask = nil
        let session = silentIncoming?.session
        silentIncoming = nil
        // Register cancellation before another call can acquire the audio lease.
        session?.cancelSilently()
    }

    // 接管媒体超时(对齐 Android takeoverMediaTimeoutMs;真机实证:失败会话不复位会挡后续 offer)。
    private let takeoverMediaTimeout: Duration = .seconds(20)
    private let offerPollInterval: Duration = .seconds(3)
    private let lineStatusInterval: Duration = .seconds(15)

    init() {
        pairing = store.load()
        rebuildClient()
        callKit.delegate = self
        callKit.start()
        registerCurrentVoipToken()
    }

    private func rebuildClient() {
        resetDeviceStatus()
        guard let p = pairing else {
            claimPrewarmer.clear()
            client = nil
            messageInbox = nil
            callHistory = nil
            return
        }
        claimPrewarmer.bind(gatewayURL: p.gatewayURL, deviceId: p.credential.deviceId)
        client = try? HostedCloudClient(baseURL: p.gatewayURL).also { $0.credential = p.credential }
        if let client {
            messageInbox = MessageInboxModel(
                client: client,
                store: messageStore,
                deviceId: p.credential.deviceId,
                onUnauthorized: { [weak self] in self?.unpair() }
            )
            callHistory = CallHistoryModel(
                client: client,
                store: callHistoryStore,
                deviceId: p.credential.deviceId,
                onUnauthorized: { [weak self] in self?.unpair() }
            )
            registerCurrentVoipToken()
        } else {
            messageInbox = nil
            callHistory = nil
        }
    }

    // MARK: - 配对

    func pair(code: String, gatewayURL: String, displayName: String) async {
        pairingError = nil
        do {
            let c = try HostedCloudClient(baseURL: gatewayURL)
            let result = try await c.claimPairing(code: code, displayName: displayName)
            let stored = StoredPairing(
                gatewayURL: gatewayURL, displayName: displayName,
                credential: result.credential, edgeId: result.edgeId
            )
            store.save(stored)
            pairing = stored
            rebuildClient()
        } catch let e as HostedCloudError {
            pairingError = PairingErrorCopy.message(code: e.code)
        } catch {
            pairingError = PairingErrorCopy.message(code: "TRANSPORT_ERROR")
        }
    }

    func unpair() {
        closeSilentIncoming()
        let pairedClient = client
        Task {
            try? await pairedClient?.unregisterVoipToken()
            if AlertPushConfiguration.isEnabled { try? await pairedClient?.unregisterAlertToken() }
        }
        messageInbox?.clearLocalData()
        callHistory?.clearLocalData()
        store.clear()
        claimPrewarmer.clear()
        pairing = nil
        client = nil
        messageInbox = nil
        callHistory = nil
        incomingOffer = nil
        resetDeviceStatus()
    }

    func clearLocalContent() {
        messageInbox?.clearLocalData()
        callHistory?.clearLocalData()
        try? messageStore.clear()
        try? callHistoryStore.clear()
    }

    func syncAlertToken(_ token: String?, authorized: Bool) async {
        guard AlertPushConfiguration.isEnabled, let client else { return }
        if !authorized {
            try? await client.unregisterAlertToken()
        } else if let token {
            try? await client.registerAlertToken(token, environment: PushEnvironment.current())
        }
    }

    func openAlert(_ route: AlertPushRoute) async {
        guard AlertPushConfiguration.isEnabled, pairing != nil else { return }
        selectedTab = route.destination == .messages ? .messages : .records
        alertRouteID = route.id
        switch route.destination {
        case .messages: await messageInbox?.refresh(waitForCurrentRequest: true)
        case .records: await callHistory?.refresh(waitForCurrentRequest: true)
        }
    }

    func loadCachedContentForSettings() {
        messageInbox?.loadCachedContent()
        callHistory?.loadCachedContent()
    }

    // MARK: - 轮询(前台版:offer + 线路状态)

    func startOfferPolling() async {
        // 两条独立节奏合一:每 offerPollInterval 拉 offer,每 5 轮拉一次线路状态。
        var tick = 0
        while !Task.isCancelled {
            if let c = client {
                if let offers = try? await c.listInboundOffers() {
                    if let pending = silentIncoming, !offers.contains(where: { $0.offerId == pending.offer.offerId }) {
                        closeSilentIncoming()
                    }
                    callKit.reconcile(
                        openOffers: offers,
                        nowUnixMs: Int64(Date().timeIntervalSince1970 * 1_000)
                    )
                    if callState == .idle, let offer = offers.first(where: {
                        !dismissedOffers.contains($0.offerId)
                            && $0.expiresAt > Int64(Date().timeIntervalSince1970 * 1000)
                    }) {
                        incomingOffer = offer
                    } else if callState == .idle {
                        incomingOffer = nil
                    }
                } else if let offer = incomingOffer,
                          offer.expiresAt <= Int64(Date().timeIntervalSince1970 * 1_000) {
                    incomingOffer = nil
                }
                if callState != .idle {
                    incomingOffer = nil
                }
                if tick % 5 == 0 { await refreshDeviceStatus() }
            }
            tick += 1
            try? await Task.sleep(for: offerPollInterval)
        }
    }

    func refreshDeviceStatus() async {
        guard !deviceStatusRefreshing, let currentClient = client else { return }
        let refresh = deviceStatusMachine.beginRefresh()
        publishDeviceStatus()
        deviceStatusRefreshing = true
        defer { deviceStatusRefreshing = false }
        do {
            let status = try await currentClient.deviceStatus()
            guard currentClient === client,
                  deviceStatusMachine.succeed(status, for: refresh) else { return }
            claimPrewarmer.setEnabled(status.claimPrewarm)
            claimPrewarmer.setPreconnectEnabled(status.preconnect)
            if !claimPrewarmer.canPreconnect { closeSilentIncoming() }
        } catch {
            guard currentClient === client else { return }
            if error is CancellationError
                || (error as? URLError)?.code == .cancelled
                || Task.isCancelled {
                guard deviceStatusMachine.cancel(for: refresh) else { return }
            } else if (error as? HostedCloudError)?.code == "UNAUTHORIZED" {
                unpair()
                return
            } else {
                guard deviceStatusMachine.fail(for: refresh) else { return }
            }
        }
        publishDeviceStatus()
    }

    func dismissOffer(_ offer: InboundOffer) {
        if silentIncoming?.offer.offerId == offer.offerId { closeSilentIncoming() }
        dismissedOffers.insert(offer.offerId)
        incomingOffer = nil
        Task { _ = await callKit.requestEndIfManaged(offer) }
    }

    // MARK: - 外呼(US-2)

    func startCall(number: String) async {
        guard let c = client, let p = pairing, callState == .idle else { return }
        closeSilentIncoming()
        let timing = CallTiming()
        timing.mark(.dialStart)
        let attempt = beginCallAttempt(with: .preparing(label: number))
        guard await canBeginAudio(label: number, attempt: attempt) else { return }
        // createSession → LiveKit 媒体 → 号码经 Dongle SIM ATD(dial 在 media_ready 后发)。
        // 具体媒体建立与 data-topic 控制由 CallMediaSession 承担。
        _ = apply(.waitingMedia(label: number), for: attempt)
        let session = CallMediaSession(onState: { [weak self] st in
            _ = self?.apply(st, for: attempt)
        }, transportFactory: { LiveKitRoomTransport() }, audio: LiveKitAudioDevice.owner, timing: timing)
        media = session
        scheduleMediaTimeout(for: attempt, session: session, duration: .seconds(30))
        await session.startOutbound(client: c, edgeId: p.edgeId, number: number)
    }

    // MARK: - 来电接管(US-1 App 侧,前台版)

    func answerTakeover(_ offer: InboundOffer) async {
        incomingOffer = nil
        if await callKit.requestAnswerIfManaged(offer) { return }
        await performTakeover(offer)
    }

    private func performTakeover(_ offer: InboundOffer, usesCallKit: Bool = false,
                                 timing: CallTiming = CallTiming()) async {
        if usesCallKit, !callKit.isAnswering(offerId: offer.offerId) { return }
        guard let c = client, callState == .idle else { return }
        incomingOffer = nil
        let label = L10n.text("call.takeover.label")
        let attempt = beginCallAttempt(with: .preparing(label: label))
        callKitOfferId = usesCallKit ? offer.offerId : nil
        guard await canBeginAudio(label: label, attempt: attempt) else { return }
        if usesCallKit, !callKit.isAnswering(offerId: offer.offerId) { return }
        _ = apply(.waitingMedia(label: label), for: attempt)
        let session: CallMediaSession
        if usesCallKit, let pending = silentIncoming, pending.offer.offerId == offer.offerId {
            session = pending.session
            silentIncoming = nil
            silentIncomingTask = nil
            silentExpiryTask?.cancel()
            silentExpiryTask = nil
            session.setStateHandler { [weak self] st in _ = self?.apply(st, for: attempt) }
        } else {
            closeSilentIncoming()
            session = CallMediaSession(onState: { [weak self] st in
                _ = self?.apply(st, for: attempt)
            }, transportFactory: { LiveKitRoomTransport() }, audio: LiveKitAudioDevice.owner, timing: timing)
        }
        media = session
        scheduleMediaTimeout(for: attempt, session: session, duration: takeoverMediaTimeout)
        await session.startTakeover(
            client: c,
            offerId: offer.offerId,
            usesCallKit: usesCallKit,
            onClaimed: { [weak self] in
                guard let self, callAttempts.isCurrent(attempt) else { throw CancellationError() }
                if usesCallKit {
                    try callKit.markClaimed(offerId: offer.offerId)
                    timing.mark(.fulfill)
                }
            },
            onConnected: { [weak self] in
                guard let self, callAttempts.isCurrent(attempt) else { return }
                mediaTimeoutTask?.cancel()
                if usesCallKit { callKit.markConnected(offerId: offer.offerId) }
            }
        )
    }

    private func scheduleMediaTimeout(
        for attempt: CallAttempt, session: CallMediaSession, duration: Duration
    ) {
        mediaTimeoutTask?.cancel()
        mediaTimeoutTask = Task { [weak self, weak session] in
            do {
                try await Task.sleep(for: duration)
            } catch {
                return
            }
            guard let self, callAttempts.isWaitingForMedia(attempt) else { return }
            session?.fail(
                reason: L10n.text("call.takeover.timeout"),
                code: callKitOfferId == nil ? "MEDIA_TIMEOUT" : "TAKEOVER_MEDIA_TIMEOUT"
            )
        }
    }

    private func scheduleCallResultDismissal(for attempt: CallAttempt, state: CallState) {
        callResultTask?.cancel()
        callResultTask = nil
        guard case .ended = state else { return }
        let duration = endedResultDisplayDuration
        callResultTask = Task { [weak self] in
            do { try await Task.sleep(for: duration) }
            catch { return }
            guard let self, callAttempts.isCurrentEndedResult(state, for: attempt) else { return }
            dismissCallResult()
        }
    }

    func dismissCallResult() {
        guard callAttempts.resetTerminal() else { return }
        callResultTask?.cancel()
        callResultTask = nil
        callState = .idle
        media = nil
        callKitOfferId = nil
        speakerphoneEnabled = false
    }

    func hangup() {
        if callKit.requestEndActiveIfManaged() { return }
        if let pending = pendingAudioAttempt {
            _ = apply(.ended(label: pending.label, reason: "local_hangup"), for: pending.attempt)
        }
        media?.requestHangup()
    }

    func sendDTMF(_ digit: String) {
        let activeMedia = media
        Task { await activeMedia?.sendDTMF(digit) }
    }

    func setSpeakerphone(_ enabled: Bool) {
        guard callState.isActive else { return }
        speakerphoneEnabled = enabled
        media?.setSpeakerphone(enabled)
    }

    private func beginCallAttempt(with initialState: CallState) -> CallAttempt {
        mediaTimeoutTask?.cancel()
        callResultTask?.cancel()
        callResultTask = nil
        callKitOfferId = nil
        let attempt = callAttempts.begin(with: initialState)
        speakerphoneEnabled = false
        callState = initialState
        return attempt
    }

    private func canBeginAudio(label: String, attempt: CallAttempt) async -> Bool {
        pendingAudioAttempt = (attempt, label)
        defer {
            if pendingAudioAttempt?.attempt == attempt { pendingAudioAttempt = nil }
        }
        let drained = await callKit.waitUntilAudioDrained(offerId: callKitOfferId)
        // Reserving .preparing before awaiting prevents a second admission.
        // Hangup/action timeout may already have ended this attempt while asleep.
        guard callAttempts.isCurrent(attempt), case .preparing = callState else { return false }
        if Task.isCancelled {
            _ = apply(.ended(label: label, reason: "setup_cancelled"), for: attempt)
            return false
        }
        guard drained else {
            _ = apply(.failed(
                label: label, reason: L10n.text("call.audio.busy"), code: "CALLKIT_AUDIO_BUSY"
            ), for: attempt)
            return false
        }
        return true
    }

    private func resetDeviceStatus() {
        deviceStatusMachine.reset()
        deviceStatusRefreshing = false
        publishDeviceStatus()
    }

    private func publishDeviceStatus() {
        deviceStatus = deviceStatusMachine.status
        deviceStatusSync = deviceStatusMachine.syncStatus
        lineReady = deviceStatusSync == .live && (deviceStatus?.lineReady ?? false)
        switch deviceStatusSync {
        case .idle, .loading:
            lineStatusLabel = L10n.text("line.status.checking")
        case .live:
            lineStatusLabel = deviceStatus?.lineReady == true
                ? L10n.text("line.status.ready")
                : (deviceStatus?.connected == false
                    ? L10n.text("line.status.edge_offline")
                    : L10n.text("line.status.sim_offline"))
        case .stale, .offline:
            lineStatusLabel = L10n.text("line.status.unavailable")
        }
    }

    @discardableResult
    private func apply(
        _ nextState: CallState,
        for attempt: CallAttempt,
        from expectedState: CallState? = nil
    ) -> Bool {
        guard callAttempts.transition(
            from: expectedState,
            to: nextState,
            for: attempt
        ) else { return false }
        callState = nextState
        scheduleCallResultDismissal(for: attempt, state: nextState)
        switch nextState {
        case .failed, .ended, .inCall:
            mediaTimeoutTask?.cancel()
            mediaTimeoutTask = nil
        default:
            break
        }
        if !nextState.isActive { speakerphoneEnabled = false }
        switch nextState {
        case .failed:
            callKit.finishActiveCall(reason: .failed)
        case let .ended(_, reason):
            if reason != "local_hangup" { callKit.finishActiveCall(reason: .remoteEnded) }
        case .idle, .preparing, .waitingMedia, .dialing, .inCall:
            break
        }
        return true
    }

    private func registerCurrentVoipToken() {
        guard let client, let token = callKit.currentToken else { return }
        Task { try? await client.registerVoipToken(token.value, environment: token.environment) }
    }
}

extension AppModel: CallKitCoordinatorDelegate {
    var callKitCanAcceptIncomingCall: Bool {
        pairing != nil && client != nil && callState == .idle
    }

    func callKitDidUpdateToken(_ token: String, environment: ApnsEnvironment) {
        guard let client else { return }
        Task { try? await client.registerVoipToken(token, environment: environment) }
    }

    func callKitDidInvalidateToken() {
        guard let client else { return }
        Task { try? await client.unregisterVoipToken() }
    }

    func callKitDidReceiveOffer(_ offer: InboundOffer) {
        // A background CallKit hangup leaves the result unacknowledged.
        dismissCallResult()
        guard callKitCanAcceptIncomingCall,
              !dismissedOffers.contains(offer.offerId) else { return }
        incomingOffer = offer
        if let client {
            if claimPrewarmer.canPreconnect {
                guard silentIncoming == nil else { return }
                let timing = CallTiming()
                timing.mark(.pushReceived, at: offer.pushReceivedAt)
                let session = CallMediaSession(onState: { _ in }, transportFactory: { LiveKitRoomTransport() },
                                               audio: LiveKitAudioDevice.owner, timing: timing)
                session.onSilentDisconnect = { [weak self] in
                    guard let self, silentIncoming?.offer.offerId == offer.offerId else { return }
                    closeSilentIncoming()
                    incomingOffer = nil
                    callKit.finishRingingOffer(offerId: offer.offerId)
                }
                silentIncoming = (offer, session)
                silentIncomingTask = Task { await session.startSilentPreconnect(client: client, offer: offer) }
                silentExpiryTask = Task {
                    do { try await Task.sleep(for: .milliseconds(max(0, offer.expiresAt - Int64(Date().timeIntervalSince1970 * 1000)))) }
                    catch { return }
                    if self.silentIncoming?.offer.offerId == offer.offerId { self.closeSilentIncoming() }
                }
            } else {
                claimPrewarmer.receive(offer) { await client.prewarmClaimConnection() }
                claimPrewarmer.timing(for: offer.offerId)?.mark(.pushReceived, at: offer.pushReceivedAt)
            }
        }
    }

    func callKitDidRequestAnswer(_ offer: InboundOffer) {
        dismissCallResult()
        guard callKitCanAcceptIncomingCall else {
            callKit.finishActiveCall(reason: .failed)
            return
        }
        let timing = silentIncoming?.session.timing ?? claimPrewarmer.timing(for: offer.offerId) ?? CallTiming()
        timing.mark(.answerAction)
        Task { await performTakeover(offer, usesCallKit: true, timing: timing) }
    }

    func callKitDidRequestDecline(_ offer: InboundOffer) {
        if silentIncoming?.offer.offerId == offer.offerId { closeSilentIncoming() }
        dismissedOffers.insert(offer.offerId)
        if incomingOffer?.offerId == offer.offerId { incomingOffer = nil }
    }

    func callKitDidRequestHangup(offerId: String) {
        if silentIncoming?.offer.offerId == offerId { closeSilentIncoming() }
        guard callKitOfferId == offerId else { return }
        if let pending = pendingAudioAttempt {
            _ = apply(.ended(label: pending.label, reason: "local_hangup"), for: pending.attempt)
            return
        }
        media?.requestHangup()
    }

    func callKitPrepareToAnswer(offerId: String) throws {
        guard callKitOfferId == offerId, let media, callState.isActive else { throw CancellationError() }
        try media.prepareCallKitAnswer()
    }

    func callKitDidActivateAudio(offerId: String) throws {
        guard callKitOfferId == offerId, let media, callState.isActive else { throw CancellationError() }
        try media.activateCallKitAudio()
    }

    func callKitDidFail(offerId: String, code: String) {
        if silentIncoming?.offer.offerId == offerId { closeSilentIncoming() }
        guard callKitOfferId == offerId else { return }
        if let pending = pendingAudioAttempt {
            _ = apply(.failed(label: pending.label, reason: L10n.text("call.audio.failed"), code: code),
                      for: pending.attempt)
            return
        }
        media?.fail(reason: L10n.text("call.audio.failed"), code: code)
    }

    func callKitAudioStopper(offerId: String) -> () -> Void {
        guard callKitOfferId == offerId, let session = media else { return {} }
        return { session.finishCallKitAudioDrain() }
    }
}

// 小工具:Kotlin `.also {}` 的 Swift 等价,链式配置。
private extension HostedCloudClient {
    func also(_ configure: (HostedCloudClient) -> Void) -> HostedCloudClient {
        configure(self); return self
    }
}
