import Foundation
import Combine

/// App 状态中枢:配对、线路状态轮询、来电 offer 轮询、通话状态机。
/// 对齐 Android CallManager + MainActivity 的组合职责。
/// 媒体会话(LiveKit)由 CallMediaSession 承担;本类只驱动状态与协议。
@MainActor
final class AppModel: ObservableObject {
    @Published var pairing: StoredPairing?
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
    private var callAttempts = CallAttemptStateMachine()
    private let messageStore = FileMessageCacheStore()
    private let callHistoryStore = FileCallHistoryCacheStore()
    private var deviceStatusMachine = DeviceStatusStateMachine()

    // 接管媒体超时(对齐 Android takeoverMediaTimeoutMs;真机实证:失败会话不复位会挡后续 offer)。
    private let takeoverMediaTimeout: Duration = .seconds(20)
    private let offerPollInterval: Duration = .seconds(3)
    private let lineStatusInterval: Duration = .seconds(15)

    init() {
        pairing = store.load()
        rebuildClient()
    }

    private func rebuildClient() {
        resetDeviceStatus()
        guard let p = pairing else {
            client = nil
            messageInbox = nil
            callHistory = nil
            return
        }
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
        messageInbox?.clearLocalData()
        callHistory?.clearLocalData()
        store.clear()
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
                if callState == .idle {
                    if let offer = try? await c.listInboundOffers().first(where: {
                        !dismissedOffers.contains($0.offerId)
                            && $0.expiresAt > Int64(Date().timeIntervalSince1970 * 1000)
                    }) {
                        incomingOffer = offer
                    } else {
                        incomingOffer = nil
                    }
                } else {
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
        dismissedOffers.insert(offer.offerId)
        incomingOffer = nil
    }

    // MARK: - 外呼(US-2)

    func startCall(number: String) async {
        guard let c = client, let p = pairing, callState == .idle else { return }
        let attempt = beginCallAttempt(with: .preparing(label: number))
        // createSession → LiveKit 媒体 → 号码经 Dongle SIM ATD(dial 在 media_ready 后发)。
        // 具体媒体建立与 data-topic 控制由 CallMediaSession 承担。
        _ = apply(.waitingMedia(label: number), for: attempt)
        media = CallMediaSession(onState: { [weak self] st in
            _ = self?.apply(st, for: attempt)
        })
        await media?.startOutbound(client: c, edgeId: p.edgeId, number: number)
    }

    // MARK: - 来电接管(US-1 App 侧,前台版)

    func answerTakeover(_ offer: InboundOffer) async {
        guard let c = client, callState == .idle else { return }
        incomingOffer = nil
        let label = L10n.text("call.takeover.label")
        let waitingState = CallState.waitingMedia(label: label)
        let attempt = beginCallAttempt(with: waitingState)
        media = CallMediaSession(onState: { [weak self] st in
            _ = self?.apply(st, for: attempt)
        })
        // 20s 媒体超时:失败结果保持可见，等待用户显式返回拨号页。
        let timeoutTask = Task { [weak self] in
            do {
                try await Task.sleep(for: self?.takeoverMediaTimeout ?? .seconds(20))
            } catch {
                return
            }
            guard let self else { return }
            let failedState = CallState.failed(
                label: label,
                reason: L10n.text("call.takeover.timeout"),
                code: "TAKEOVER_MEDIA_TIMEOUT"
            )
            guard self.apply(failedState, for: attempt, from: waitingState) else { return }
            await self.media?.stop()
        }
        await media?.startTakeover(client: c, offerId: offer.offerId, onConnected: { timeoutTask.cancel() })
    }

    func dismissCallResult() {
        guard callAttempts.resetTerminal() else { return }
        callState = .idle
        media = nil
        speakerphoneEnabled = false
    }

    func hangup() {
        let activeMedia = media
        Task { await activeMedia?.hangup() }
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
        let attempt = callAttempts.begin(with: initialState)
        speakerphoneEnabled = false
        callState = initialState
        return attempt
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
        if !nextState.isActive { speakerphoneEnabled = false }
        return true
    }
}

// 小工具:Kotlin `.also {}` 的 Swift 等价,链式配置。
private extension HostedCloudClient {
    func also(_ configure: (HostedCloudClient) -> Void) -> HostedCloudClient {
        configure(self); return self
    }
}
