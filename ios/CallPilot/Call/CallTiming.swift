import Foundation
import CoreFoundation
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
    func httpBatches(reservingBytes: Int = 0) -> [Data] {
        sent.removeAll()
        sentCloudDurations.removeAll()
        var result: [Data] = []
        var bytes = 128 // claimId, envelope, and array separators
        while result.count < 8, let packet = nextBatch(isHTTPReplay: true) {
            guard bytes + packet.count + 1 <= 4096 - max(0, reservingBytes) else { break }
            result.append(packet)
            bytes += packet.count + 1
        }
        return result
    }
}

// The SDK report is consumed locally. Only this explicit allowlist reaches the
// wire; candidate identifiers, addresses, SSRC and raw SDP never leave it.
struct CallRtpReport {
    let id: String
    let type: String
    let values: [String: Any]
}

enum CallRtpStats {
    static let maxBytes = 512
    private static let maxInteger = 9_007_199_254_740_991.0

    static func packet(reports: [CallRtpReport], sequence: Int, elapsedMs: Int) -> Data? {
        guard (1...10).contains(sequence), (0...60_000).contains(elapsedMs) else { return nil }
        let audio = reports.filter { ($0.values["kind"] as? String ?? $0.values["mediaType"] as? String) == "audio" }
        let outbound = audio.filter { $0.type == "outbound-rtp" }
        let inbound = audio.filter { $0.type == "inbound-rtp" }
        let remote = audio.filter { $0.type == "remote-inbound-rtp" }
        let pairID = reports.first(where: { $0.type == "transport" && $0.values["selectedCandidatePairId"] is String })?.values["selectedCandidatePairId"] as? String
        let pair = reports.first(where: { $0.type == "candidate-pair" && $0.id == pairID }) ?? reports.first(where: {
            $0.type == "candidate-pair" && (($0.values["selected"] as? Bool == true) ||
                ($0.values["nominated"] as? Bool == true && $0.values["state"] as? String == "succeeded"))
        })
        let localID = pair?.values["localCandidateId"] as? String
        let remoteID = pair?.values["remoteCandidateId"] as? String
        let candidates = Set(["host", "srflx", "prflx", "relay"])
        let states = Set(["frozen", "waiting", "in-progress", "failed", "succeeded"])
        let rtt = remote.compactMap { finiteNumber($0.values["roundTripTime"], minimum: 0) }.first
        let fields: [String: Any] = [
            "type": "client_rtp_stats", "v": 1, "seq": sequence, "elapsedMs": elapsedMs,
            "outPackets": sum(outbound, "packetsSent") ?? NSNull(),
            "outBytes": sum(outbound, "bytesSent") ?? NSNull(),
            "inPackets": sum(inbound, "packetsReceived") ?? NSNull(),
            "inBytes": sum(inbound, "bytesReceived") ?? NSNull(),
            "lost": sum(inbound, "packetsLost", signed: true) ?? NSNull(),
            "rttMs": rtt.flatMap { $0 * 1000 <= Double.greatestFiniteMagnitude ? $0 * 1000 : nil } as Any? ?? NSNull(),
            "localCandidate": allowed(reports.first(where: { $0.id == localID })?.values["candidateType"], in: candidates) ?? NSNull(),
            "remoteCandidate": allowed(reports.first(where: { $0.id == remoteID })?.values["candidateType"], in: candidates) ?? NSNull(),
            "pairState": allowed(pair?.values["state"], in: states) ?? NSNull(),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
              data.count <= maxBytes else { return nil }
        return data
    }

    static func validatedObject(_ data: Data?) -> [String: Any]? {
        guard let data, data.count <= maxBytes,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == ["type", "v", "seq", "elapsedMs", "outPackets", "outBytes", "inPackets", "inBytes", "lost", "rttMs", "localCandidate", "remoteCandidate", "pairState"],
              object["type"] as? String == "client_rtp_stats",
              finiteNumber(object["v"], minimum: 1) == 1,
              let sequence = finiteNumber(object["seq"], minimum: 1), sequence <= 10, sequence.rounded(.towardZero) == sequence,
              let elapsed = finiteNumber(object["elapsedMs"], minimum: 0), elapsed <= 60_000, elapsed.rounded(.towardZero) == elapsed else { return nil }
        for name in ["outPackets", "outBytes", "inPackets", "inBytes", "lost"] {
            if object[name] is NSNull { continue }
            guard let value = finiteNumber(object[name], minimum: name == "lost" ? -maxInteger : 0),
                  value <= maxInteger, value.rounded(.towardZero) == value else { return nil }
        }
        if !(object["rttMs"] is NSNull), finiteNumber(object["rttMs"], minimum: 0) == nil { return nil }
        for name in ["localCandidate", "remoteCandidate"] {
            if !(object[name] is NSNull), allowed(object[name], in: ["host", "srflx", "prflx", "relay"]) == nil { return nil }
        }
        if !(object["pairState"] is NSNull), allowed(object["pairState"], in: ["frozen", "waiting", "in-progress", "failed", "succeeded"]) == nil { return nil }
        return object
    }

    private static func finiteNumber(_ value: Any?, minimum: Double) -> Double? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let value = number.doubleValue
        return value.isFinite && value >= minimum ? value : nil
    }

    private static func sum(_ reports: [CallRtpReport], _ key: String, signed: Bool = false) -> Any? {
        guard !reports.isEmpty else { return nil }
        var total = 0.0
        for report in reports {
            guard let value = finiteNumber(report.values[key], minimum: signed ? -maxInteger : 0),
                  abs(value) <= maxInteger, value.rounded(.towardZero) == value else { return nil }
            total += value
            guard abs(total) <= maxInteger else { return nil }
        }
        return Int64(total)
    }

    private static func allowed(_ value: Any?, in choices: Set<String>) -> Any? {
        guard let value = value as? String, choices.contains(value) else { return nil }
        return value
    }
}

@MainActor
final class CallRtpSampler {
    static let interval: Duration = .seconds(2)
    private let now: () -> TimeInterval
    private let sleep: (Duration) async throws -> Void
    private let request: (Int, Int) -> Void
    private var task: Task<Void, Never>?
    private var origin: TimeInterval?
    private var sequence = 0
    private var finished = false

    init(now: @escaping () -> TimeInterval = CallMonotonic.now,
         sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) },
         request: @escaping (Int, Int) -> Void) {
        self.now = now
        self.sleep = sleep
        self.request = request
    }

    func start() {
        guard origin == nil, !finished else { return }
        origin = now()
        sample()
        task = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled, !finished, sequence < 10 {
                do { try await sleep(Self.interval) } catch { return }
                guard !Task.isCancelled, !finished else { return }
                sample()
            }
        }
    }

    func finish() {
        guard !finished else { return }
        finished = true
        task?.cancel()
        task = nil
        // A commit callback can win the main-actor race against the connected
        // continuation. It still needs a terminal observation, never a timer.
        if origin == nil { origin = now() }
        sample()
    }

    private func sample() {
        guard let origin, sequence < 10 else { return }
        sequence += 1
        request(sequence, Int(min(60_000, max(0, (now() - origin) * 1000))))
    }
}
