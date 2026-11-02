import Foundation
import OSLog

enum CallTimingMark: String, CaseIterable, Sendable {
    case answerAction = "answer_action", dialStart = "dial_start"
    case claimStart = "claim_start", claimResponse = "claim_response"
    case claimDNSStart = "claim_dns_start", claimDNSEnd = "claim_dns_end"
    case claimConnectStart = "claim_connect_start", claimConnectEnd = "claim_connect_end"
    case claimTLSStart = "claim_tls_start", claimTLSEnd = "claim_tls_end"
    case claimRequestStart = "claim_request_start", claimFirstByte = "claim_first_byte"
    case claimConnectionReused = "claim_connection_reused"
    case prewarmStart = "prewarm_start", prewarmEnd = "prewarm_end"
    case configureStart = "configure_start", configureEnd = "configure_end", fulfill
    case didActivate = "did_activate", activateReturn = "activate_return"
    case connectStart = "connect_start", connectReturn = "connect_return"
    case roomConnected = "room_connected", audioActivated = "audio_activated"
    case micEnableStart = "mic_enable_start", micEnableReturn = "mic_enable_return"
    case downlinkSubscribed = "downlink_subscribed", downlinkFirstFrame = "downlink_first_frame"
    case edgeConnected = "edge_connected", hangupIntent = "hangup_intent"
    case hangupPublishStart = "hangup_publish_start", hangupPublishEnd = "hangup_publish_end"
    case hangupPublishError = "hangup_publish_error", hangupRetryStart = "hangup_retry_start"
    case hangupRetryEnd = "hangup_retry_end", hangupRetryError = "hangup_retry_error"
    case hangupAck = "hangup_ack", disconnect
}

enum CallMonotonic {
    private static let origin = ContinuousClock.now
    static func now() -> TimeInterval {
        let duration = origin.duration(to: .now).components
        return Double(duration.seconds) + Double(duration.attoseconds) / 1e18
    }
}

@MainActor
final class CallTiming {
    static let maxBatchMarks = 16
    static let maxBatchBytes = 2048
    static let maxBatches = 64
    let attempt = UUID().uuidString
    private let now: () -> TimeInterval
    private var origin: TimeInterval
    private var baselineKnown = false
    private var marks: [CallTimingMark: TimeInterval] = [:]
    private var sent = Set<CallTimingMark>()
    private(set) var sequence = 0
    private let logger = Logger(subsystem: "ai.bondings.callpilot.sim", category: "CallTiming")

    init(now: @escaping () -> TimeInterval = CallMonotonic.now) {
        self.now = now
        origin = now()
    }

    func mark(_ stage: CallTimingMark, at instant: TimeInterval? = nil) {
        guard marks[stage] == nil else { return }
        let instant = instant ?? now()
        marks[stage] = instant
        if stage == .dialStart { baselineKnown = true }
        if baselineKnown || stage == .hangupIntent || stage == .disconnect {
            log(stage, at: instant)
        }
    }

    private func log(_ stage: CallTimingMark, at instant: TimeInterval) {
        // Keep teardown observations locally even when the data channel is already gone.
        let ms = (instant - origin) * 1000
        logger.info("attempt=\(self.attempt, privacy: .public) stage=\(stage.rawValue, privacy: .public) ms=\(ms, privacy: .public)")
    }

    func startClaim() {
        origin = now()
        baselineKnown = true
        for (stage, instant) in marks { log(stage, at: instant) }
        mark(.claimStart, at: origin)
    }

    func nextBatch() -> Data? {
        guard sequence < Self.maxBatches else { return nil }
        let keys = CallTimingMark.allCases.filter { marks[$0] != nil && !sent.contains($0) }
            .prefix(Self.maxBatchMarks)
        guard !keys.isEmpty else { return nil }
        let values = Dictionary(uniqueKeysWithValues: keys.map {
            ($0.rawValue, min(86_400_000, max(-60_000, (marks[$0]! - origin) * 1000)))
        })
        guard let data = try? JSONSerialization.data(withJSONObject: [
            "type": "client_timing", "v": 1, "attempt": attempt,
            "seq": sequence + 1, "marks": values,
        ], options: [.sortedKeys]), data.count <= Self.maxBatchBytes else { return nil }
        sequence += 1
        sent.formUnion(keys)
        return data
    }
}
