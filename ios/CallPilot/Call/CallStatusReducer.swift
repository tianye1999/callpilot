import Foundation

enum CallStatusAction: Equatable {
    case mediaReady
    case state(CallState)
    case ignored
}

/// Converts Edge wire events into deterministic call-state actions. It never
/// reads transcript text or performs I/O, which keeps protocol evolution and
/// lifecycle behavior independently testable.
enum CallStatusReducer {
    static func reduce(_ event: EdgeCallEvent, label: String) -> CallStatusAction {
        let status: String
        let reason: String?
        let code: String?
        switch event {
        case let .status(name, eventReason, eventCode):
            status = name
            reason = eventReason
            code = eventCode
        case let .remoteCall(remoteStatus):
            status = remoteStatus
            reason = nil
            code = nil
        }

        switch status {
        case "media_ready":
            return .mediaReady
        case "dialing":
            return .state(.dialing(number: label))
        case "connected":
            return .state(.inCall(label: label))
        case "ended", "hangup":
            return .state(.ended(label: label, reason: reason ?? status))
        case "failed":
            return .state(.failed(
                label: label,
                reason: reason ?? code ?? status,
                code: code
            ))
        default:
            return .ignored
        }
    }
}
