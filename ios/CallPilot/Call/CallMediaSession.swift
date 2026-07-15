import Foundation

/// LiveKit 媒体会话(对齐 Android CallManager 的媒体+事件消费部分)。
///
/// 骨架:定义外呼/接管两条入口与状态回调边界。LiveKit iOS SDK(`import LiveKit`)
/// 的房间连接、麦克风发布、Edge status data-topic 消费在此填充——待 Xcode + SPM
/// 拉到 client-sdk-swift 后接线(见 project.yml packages.LiveKit)。
///
/// 关键契约(源自今日 Android 真机教训,iOS 必须对齐):
/// - 外呼:media_ready(Edge status 事件)后才发 dial;
/// - 接管:不发 dial(物理通话已在),入房即等 Edge 的 status=connected 推进 inCall;
/// - connected 事件消费必须早于/取消 20s 媒体超时(AppModel 侧已处理超时);
/// - 单一下行 writer、单 finalizer 由 Edge 侧保证,App 只反映状态。
@MainActor
final class CallMediaSession {
    private let onState: (CallState) -> Void
    private var connectedHandler: (() -> Void)?
    private(set) var preparedSession: HostedCallSession?
    private(set) var pendingDialPacket: Data?
    private var activeLabel: String?
    private var isStopped = false

    init(onState: @escaping (CallState) -> Void) {
        self.onState = onState
    }

    /// US-2 外呼:createSession(HTTP)→ 连房间发麦 → media_ready 后 Edge 侧 ATD。
    func startOutbound(client: HostedCloudClient, edgeId: String, number: String) async {
        isStopped = false
        activeLabel = number
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        do {
            // Validate before allocating a cloud room. The packet stays pending
            // until Edge reports media_ready, then the LiveKit layer publishes it once.
            let dialPacket = try CallSignaling.encodeDial(
                number: number,
                idempotencyKey: UUID().uuidString
            )
            let session = try await client.createSession(edgeId: edgeId)
            guard !isStopped else { return }
            preparedSession = session
            pendingDialPacket = dialPacket
            onState(.waitingMedia(label: number))
            // TODO(LiveKit): Room.connect(session.livekitURL, session.token)
            //   → publish microphone → consume callpilot.status data packets.
        } catch let error as HostedCloudError {
            guard !isStopped else { return }
            isStopped = true
            connectedHandler = nil
            onState(.failed(label: number, reason: error.message, code: error.code))
        } catch {
            guard !isStopped else { return }
            isStopped = true
            connectedHandler = nil
            onState(.failed(label: number, reason: error.localizedDescription, code: nil))
        }
    }

    /// US-1 接管:claimInboundOffer(HTTP)→ 入房(不发 dial)→ 等 Edge status=connected。
    func startTakeover(client: HostedCloudClient, offerId: String, onConnected: @escaping () -> Void) async {
        isStopped = false
        activeLabel = "来电接管"
        preparedSession = nil
        pendingDialPacket = nil
        installConnectedHandler(onConnected)
        do {
            let session = try await client.claimInboundOffer(offerId: offerId)
            guard !isStopped else { return }
            preparedSession = session
            // TODO(LiveKit): Room.connect(session.livekitURL, session.token) → 等 connected
            onState(.waitingMedia(label: "来电接管"))
        } catch let e as HostedCloudError {
            guard !isStopped else { return }
            isStopped = true
            connectedHandler = nil
            onState(.failed(label: "来电接管", reason: e.message, code: e.code))
        } catch {
            guard !isStopped else { return }
            isStopped = true
            connectedHandler = nil
            onState(.failed(label: "来电接管", reason: error.localizedDescription, code: nil))
        }
    }

    /// Edge status=connected 到达时调用(取消超时 + 推进 inCall)。
    func handleConnected(label: String) {
        guard !isStopped else { return }
        connectedHandler?()
        connectedHandler = nil
        onState(.inCall(label: label))
    }

    /// Entry point for future LiveKit data-topic delivery. Parsing and state
    /// reduction are complete here; only the Room callback wiring is pending.
    @discardableResult
    func handleEdgePayload(_ data: Data, label: String) -> CallStatusAction? {
        guard !isStopped else { return nil }
        activeLabel = label
        guard let event = CallSignaling.decodeEvent(data) else { return nil }
        let action = CallStatusReducer.reduce(event, label: label)
        switch action {
        case .mediaReady, .ignored:
            break
        case let .state(state):
            if case .inCall = state {
                handleConnected(label: label)
            } else {
                if !state.isActive {
                    isStopped = true
                    connectedHandler = nil
                    preparedSession = nil
                    pendingDialPacket = nil
                }
                onState(state)
            }
        }
        return action
    }

    /// Atomically consumes the deferred dial command. Repeated media_ready
    /// events therefore cannot redial the same physical line.
    func takePendingDialPacket() -> Data? {
        defer { pendingDialPacket = nil }
        return pendingDialPacket
    }

    func installConnectedHandler(_ handler: @escaping () -> Void) {
        connectedHandler = handler
    }

    @discardableResult
    func sendDTMF(_ digit: String) -> Data? {
        guard !isStopped else { return nil }
        guard let packet = try? CallSignaling.encodeDTMF(digit) else { return nil }
        // TODO(LiveKit): publish packet reliably on CallPilotTopic.control.
        return packet
    }

    @discardableResult
    func hangup() -> Data {
        let packet = CallSignaling.encodeHangup()
        guard !isStopped else { return packet }
        let label = activeLabel ?? "通话"
        isStopped = true
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        // TODO(LiveKit): publish packet reliably on CallPilotTopic.control, then disconnect.
        onState(.ended(label: label, reason: "local_hangup"))
        return packet
    }

    func stop() {
        isStopped = true
        connectedHandler = nil
        preparedSession = nil
        pendingDialPacket = nil
        activeLabel = nil
        // TODO(LiveKit): Room.disconnect().
    }
}
