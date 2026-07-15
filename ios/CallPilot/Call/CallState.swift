import Foundation

/// 通话 UI 状态(对齐 Android CallManager.CallState)。
/// 单通互斥,与 Edge 的一 SIM 一通对应。
enum CallState: Equatable {
    case idle
    case preparing(label: String)
    case waitingMedia(label: String)
    case dialing(number: String)
    case inCall(label: String)
    case ended(label: String, reason: String)
    case failed(label: String, reason: String, code: String?)

    /// 生命周期内(非空闲/结束/失败)——用于单通互斥与轮询门禁。
    var isActive: Bool {
        switch self {
        case .idle, .ended, .failed: return false
        default: return true
        }
    }
}
