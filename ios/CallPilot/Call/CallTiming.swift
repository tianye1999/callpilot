import Foundation
import OSLog

enum CallTimingMark: String, CaseIterable, Sendable {
    case answerAction = "answer_action", dialStart = "dial_start"
    case claimStart = "claim_start", claimResponse = "claim_response"
    case sdpSubmitStart = "sdp_submit_start", sdpSubmitResponse = "sdp_submit_response"
    case claimResponseEnd = "claim_response_end", pushReceived = "push_received"
    case prepareStart = "prepare_start", prepareResponse = "prepare_response"
    case audioLeaseStart = "audio_lease_start", audioLeaseEnd = "audio_lease_end"
    case preconnectStart = "preconnect_start", preconnectReady = "preconnect_ready"
    case permissionReady = "permission_ready", transportAdopted = "transport_adopted"
    case coldFallback = "cold_fallback"
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
    case directControlReady = "direct_control_ready"
    case audioEngineRunning = "audio_engine_running", audioEngineFailed = "audio_engine_failed"
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
    private var cloudDurations: [String: Double] = [:]
    private var sentCloudDurations = Set<String>()
    private(set) var sequence = 0
    private var audioEngine: CallAudioEngineSnapshot?
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

    func recordCloudDurations(_ header: String?) {
        guard let header, header.utf8.count <= 4096 else { return }
        let allowed: Set<String> = ["cas", "edge_reserve", "turn", "sdp_relay", "claim_finalize", "total"]
        for metric in header.split(separator: ",") {
            let parts = metric.split(separator: ";").map { $0.trimmingCharacters(in: .whitespaces) }
            guard let name = parts.first, allowed.contains(name) else { continue }
            let values = parts.dropFirst().compactMap { parameter -> String? in
                let pair = parameter.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                return pair.count == 2 && pair[0] == "dur" ? pair[1] : nil
            }
            guard values.count == 1, let value = Double(values[0]), value.isFinite,
                  (0...86_400_000).contains(value) else { continue }
            let key = "cloud_\(name)_ms"
            if cloudDurations[key] == nil { cloudDurations[key] = value }
        }
    }

    func nextBatch(isHTTPReplay: Bool = false) -> Data? {
        // Reserve two complete snapshots for fallback and terminal HTTP. A
        // milestone-per-packet channel must not exhaust the shared seq limit.
        let reserved = !isHTTPReplay && marks[.directControlReady] != nil ? 8 : 0
        guard sequence < Self.maxBatches - reserved else { return nil }
        let keys = CallTimingMark.allCases.filter { marks[$0] != nil && !sent.contains($0) }
            .prefix(Self.maxBatchMarks)
        let cloudKeys = cloudDurations.keys.sorted().filter { !sentCloudDurations.contains($0) }
            .prefix(Self.maxBatchMarks - keys.count)
        guard !keys.isEmpty || !cloudKeys.isEmpty else { return nil }
        var values = Dictionary(uniqueKeysWithValues: keys.map {
            ($0.rawValue, min(86_400_000, max(-60_000, (marks[$0]! - origin) * 1000)))
        })
        for key in cloudKeys { values[key] = cloudDurations[key] }
        var envelope: [String: Any] = [
            "type": "client_timing", "v": 1, "attempt": attempt,
            "seq": sequence + 1, "marks": values,
        ]
        if let audioEngine, let encoded = try? JSONEncoder().encode(audioEngine),
           let object = try? JSONSerialization.jsonObject(with: encoded) { envelope["audioEngine"] = object }
        guard let data = try? JSONSerialization.data(withJSONObject: envelope,
            options: [.sortedKeys]), data.count <= Self.maxBatchBytes else { return nil }
        sequence += 1
        sent.formUnion(keys)
        sentCloudDurations.formUnion(cloudKeys)
        return data
    }

    func recordAudioEngine(_ snapshot: CallAudioEngineSnapshot) { audioEngine = snapshot }

    // A successful data-channel send is not a receipt. Repack the complete
    // bounded milestone set with fresh sequence numbers for the HTTP fallback.
    func httpBatches() -> [Data] {
        sent.removeAll()
        sentCloudDurations.removeAll()
        var result: [Data] = []
        var bytes = 128 // claimId, envelope, and array separators
        while result.count < 8, let packet = nextBatch(isHTTPReplay: true) {
            guard bytes + packet.count + 1 <= 4096 else { break }
            result.append(packet)
            bytes += packet.count + 1
        }
        return result
    }
}
