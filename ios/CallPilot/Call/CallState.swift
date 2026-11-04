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

    var isTerminal: Bool {
        switch self {
        case .ended, .failed: return true
        default: return false
        }
    }

    /// Keeps the call surface visible until the result is dismissed.
    var isCallPresented: Bool {
        self != .idle
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

    func isWaitingForMedia(_ attempt: CallAttempt) -> Bool {
        guard isCurrent(attempt), case .waitingMedia = state else { return false }
        return true
    }

    func isCurrentEndedResult(_ expectedState: CallState, for attempt: CallAttempt) -> Bool {
        guard isCurrent(attempt), case .ended = state else { return false }
        return state == expectedState
    }

    /// J1：结束/失败结果都可用同一代令牌围栏，倒计时不会清掉后来开始的新通话。
    func isCurrentTerminalResult(_ expectedState: CallState, for attempt: CallAttempt) -> Bool {
        guard isCurrent(attempt), state.isTerminal else { return false }
        return state == expectedState
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

    @discardableResult
    mutating func resetTerminal() -> Bool {
        guard state.isTerminal else { return false }
        invalidate()
        return true
    }
}

/// J1:结束页自动返回拨号页的纯倒计时。tick 每秒推进一次,到零返回 true;
/// cancel 后不再推进(用户点按/滚动结束页即取消)。纯函数便于无时钟单测。
struct CallResultCountdown: Equatable {
    private(set) var remaining: Int
    private(set) var cancelled = false

    init(seconds: Int) {
        remaining = max(1, seconds)
    }

    @discardableResult
    mutating func tick() -> Bool {
        guard !cancelled else { return false }
        if remaining <= 1 {
            remaining = 0
            return true
        }
        remaining -= 1
        return false
    }

    mutating func cancel() {
        cancelled = true
        remaining = 0
    }
}
