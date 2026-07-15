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

/// Identifies one logical call attempt. Async completions must present the
/// token issued when that attempt began before they may mutate UI state.
struct CallAttempt: Equatable, Sendable {
    fileprivate let generation: UInt64
}

/// Pure state owner used to fence late timeout/network completions from an
/// older call attempt. AppModel wiring is intentionally separate from the
/// state contract so this logic can be exhaustively unit tested.
struct CallAttemptStateMachine {
    private(set) var state: CallState = .idle
    private(set) var generation: UInt64 = 0

    mutating func begin(with initialState: CallState) -> CallAttempt {
        generation &+= 1
        state = initialState
        return CallAttempt(generation: generation)
    }

    func isCurrent(_ attempt: CallAttempt) -> Bool {
        attempt.generation == generation
    }

    @discardableResult
    mutating func transition(
        from expectedState: CallState? = nil,
        to nextState: CallState,
        for attempt: CallAttempt
    ) -> Bool {
        guard isCurrent(attempt) else { return false }
        if let expectedState, state != expectedState { return false }
        state = nextState
        return true
    }

    mutating func invalidate(to nextState: CallState = .idle) {
        generation &+= 1
        state = nextState
    }
}
