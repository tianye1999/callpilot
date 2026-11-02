import Foundation

@MainActor
final class ClaimPrewarmer {
    private static let cacheKey = "claimPrewarm"
    private let defaults: UserDefaults
    private let nowMilliseconds: () -> Int64
    private var gatewayURL: String?
    private var deviceId: String?
    private var seen: [String: Int64] = [:]
    private var entries: [String: (timing: CallTiming, task: Task<Void, Never>)] = [:]
    private(set) var enabled = false

    init(defaults: UserDefaults = .standard, nowMilliseconds: @escaping () -> Int64 = {
        Int64(Date().timeIntervalSince1970 * 1_000)
    }) {
        self.defaults = defaults
        self.nowMilliseconds = nowMilliseconds
    }

    func bind(gatewayURL: String, deviceId: String) {
        guard self.gatewayURL != gatewayURL || self.deviceId != deviceId else { return }
        entries.values.forEach { $0.task.cancel() }
        entries.removeAll()
        seen.removeAll()
        self.gatewayURL = gatewayURL
        self.deviceId = deviceId
        let cached = defaults.dictionary(forKey: Self.cacheKey)
        let matches = cached?["gatewayURL"] as? String == gatewayURL && cached?["deviceId"] as? String == deviceId
        enabled = matches && cached?["enabled"] as? Bool == true
        if matches, let offers = cached?["seen"] as? [String: Int64] {
            seen = offers.filter { $0.value > nowMilliseconds() }
        }
    }

    func setEnabled(_ enabled: Bool) {
        self.enabled = enabled
        if !enabled { entries.values.forEach { $0.task.cancel() } }
        persist()
    }

    @discardableResult
    func receive(_ offer: InboundOffer, prewarm: @escaping @MainActor () async -> Void) -> Task<Void, Never>? {
        let now = nowMilliseconds()
        for (id, expiry) in seen where expiry <= now {
            entries.removeValue(forKey: id)?.task.cancel()
            seen.removeValue(forKey: id)
        }
        guard enabled, gatewayURL != nil, deviceId != nil, offer.expiresAt > now,
              seen[offer.offerId] == nil, seen.count < 64 else { return nil }
        seen[offer.offerId] = offer.expiresAt
        persist()
        let timing = CallTiming()
        let task = Task {
            guard !Task.isCancelled else { return }
            timing.mark(.prewarmStart)
            await prewarm()
            timing.mark(.prewarmEnd)
        }
        entries[offer.offerId] = (timing, task)
        return task
    }

    func timing(for offerId: String) -> CallTiming? { entries[offerId]?.timing }

    func clear() {
        entries.values.forEach { $0.task.cancel() }
        entries.removeAll()
        seen.removeAll()
        gatewayURL = nil
        deviceId = nil
        enabled = false
        defaults.removeObject(forKey: Self.cacheKey)
    }

    private func persist() {
        guard let gatewayURL, let deviceId else { return }
        defaults.set([
            "gatewayURL": gatewayURL, "deviceId": deviceId, "enabled": enabled, "seen": seen,
        ], forKey: Self.cacheKey)
    }
}
