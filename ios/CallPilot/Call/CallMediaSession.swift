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

    init(onState: @escaping (CallState) -> Void) {
        self.onState = onState
    }

    /// US-2 外呼:createSession(HTTP)→ 连房间发麦 → media_ready 后 Edge 侧 ATD。
    func startOutbound(client: HostedCloudClient, edgeId: String, number: String) async {
        // TODO(LiveKit): client.createSession(edgeId:) → Room.connect(url, token)
        //   → 发布麦克风轨 → 消费 status 事件(media_ready/dialing/connected/ended)。
        // 当前骨架:标记等待媒体,待 SDK 接线。
        onState(.waitingMedia(label: number))
    }

    /// US-1 接管:claimInboundOffer(HTTP)→ 入房(不发 dial)→ 等 Edge status=connected。
    func startTakeover(client: HostedCloudClient, offerId: String, onConnected: @escaping () -> Void) async {
        connectedHandler = onConnected
        do {
            let session = try await client.claimInboundOffer(offerId: offerId)
            _ = session  // TODO(LiveKit): Room.connect(session.livekitURL, session.token) → 等 connected
            onState(.waitingMedia(label: "来电接管"))
        } catch let e as HostedCloudError {
            onState(.failed(label: "来电接管", reason: e.message, code: e.code))
        } catch {
            onState(.failed(label: "来电接管", reason: error.localizedDescription, code: nil))
        }
    }

    /// Edge status=connected 到达时调用(取消超时 + 推进 inCall)。
    func handleConnected(label: String) {
        connectedHandler?()
        connectedHandler = nil
        onState(.inCall(label: label))
    }

    func sendDTMF(_ digit: String) {
        // TODO(LiveKit): 经 data-topic 发 {type:dtmf,digits:digit}(对齐 Android sendDtmf)。
    }

    func hangup() {
        // TODO(LiveKit): 经 data-topic 发 hangup + Room.disconnect()。
        onState(.ended(label: "通话", reason: "local_hangup"))
    }

    func stop() {
        // TODO(LiveKit): Room.disconnect() + 清理。
    }
}
