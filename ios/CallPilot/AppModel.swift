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
    @Published var lineStatusLabel = "线路状态获取中…"
    @Published var lineReady = false

    private let store = CredentialStore()
    private var client: HostedCloudClient?
    private var dismissedOffers = Set<String>()
    private var media: CallMediaSession?

    // 接管媒体超时(对齐 Android takeoverMediaTimeoutMs;真机实证:失败会话不复位会挡后续 offer)。
    private let takeoverMediaTimeout: Duration = .seconds(20)
    private let offerPollInterval: Duration = .seconds(3)
    private let lineStatusInterval: Duration = .seconds(15)

    init() {
        pairing = store.load()
        rebuildClient()
    }

    private func rebuildClient() {
        guard let p = pairing else { client = nil; return }
        client = try? HostedCloudClient(baseURL: p.gatewayURL).also { $0.credential = p.credential }
    }

    // MARK: - 配对

    func pair(code: String, gatewayURL: String, displayName: String) async {
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
            lineStatusLabel = "配对失败:\(e.message)"
        } catch {
            lineStatusLabel = "配对失败:\(error.localizedDescription)"
        }
    }

    func unpair() {
        store.clear()
        pairing = nil
        client = nil
        incomingOffer = nil
    }

    // MARK: - 轮询(前台版:offer + 线路状态)

    func startOfferPolling() async {
        // 两条独立节奏合一:每 offerPollInterval 拉 offer,每 5 轮拉一次线路状态。
        var tick = 0
        while !Task.isCancelled {
            if let c = client {
                if !callState.isActive {
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
                if tick % 5 == 0, let status = try? await c.deviceStatus() {
                    lineReady = status.lineReady
                    lineStatusLabel = status.lineReady ? "远程拨号已就绪"
                        : (!status.connected ? "电脑端离线" : "SIM 线路离线")
                }
            }
            tick += 1
            try? await Task.sleep(for: offerPollInterval)
        }
    }

    func dismissOffer(_ offer: InboundOffer) {
        dismissedOffers.insert(offer.offerId)
        incomingOffer = nil
    }

    // MARK: - 外呼(US-2)

    func startCall(number: String) async {
        guard let c = client, let p = pairing, !callState.isActive else { return }
        callState = .preparing(label: number)
        // createSession → LiveKit 媒体 → 号码经 Dongle SIM ATD(dial 在 media_ready 后发)。
        // 具体媒体建立由 CallMediaSession 承担,占位待 LiveKit 接线。
        callState = .waitingMedia(label: number)
        media = CallMediaSession(onState: { [weak self] st in
            Task { @MainActor in self?.callState = st }
        })
        await media?.startOutbound(client: c, edgeId: p.edgeId, number: number)
    }

    // MARK: - 来电接管(US-1 App 侧,前台版)

    func answerTakeover(_ offer: InboundOffer) async {
        guard let c = client, !callState.isActive else { return }
        incomingOffer = nil
        let label = "来电接管"
        callState = .waitingMedia(label: label)
        media = CallMediaSession(onState: { [weak self] st in
            Task { @MainActor in self?.callState = st }
        })
        // 20s 媒体超时:对齐 Android——接管失败不复位会永久 WaitingMedia 挡后续 offer。
        let timeoutTask = Task { [weak self] in
            try? await Task.sleep(for: self?.takeoverMediaTimeout ?? .seconds(20))
            guard let self, case .waitingMedia = self.callState else { return }
            self.callState = .failed(label: label, reason: "接管媒体建立超时", code: "TAKEOVER_MEDIA_TIMEOUT")
            self.media?.stop()
            try? await Task.sleep(for: .seconds(2))
            if case .failed = self.callState { self.callState = .idle }
        }
        await media?.startTakeover(client: c, offerId: offer.offerId, onConnected: { timeoutTask.cancel() })
    }

    func hangup() {
        media?.hangup()
    }

    func sendDTMF(_ digit: String) {
        media?.sendDTMF(digit)
    }
}

// 小工具:Kotlin `.also {}` 的 Swift 等价,链式配置。
private extension HostedCloudClient {
    func also(_ configure: (HostedCloudClient) -> Void) -> HostedCloudClient {
        configure(self); return self
    }
}
