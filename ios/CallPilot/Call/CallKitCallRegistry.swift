import Foundation

struct CallKitCallRegistry {
    enum Phase: Equatable {
        case ringing
        case answering
        case active
    }

    private struct Entry {
        let payload: VoipPushPayload
        var phase: Phase
    }

    private var calls: [UUID: Entry] = [:]

    mutating func register(_ payload: VoipPushPayload, nowUnixMs: Int64) -> Bool {
        guard payload.expiresAtUnixMs > nowUnixMs,
              calls[payload.callUUID] == nil,
              !calls.values.contains(where: { $0.payload.offerId == payload.offerId })
        else { return false }
        calls[payload.callUUID] = Entry(payload: payload, phase: .ringing)
        return true
    }

    mutating func beginAnswer(callUUID: UUID) -> VoipPushPayload? {
        guard var entry = calls[callUUID], entry.phase == .ringing else { return nil }
        entry.phase = .answering
        calls[callUUID] = entry
        return entry.payload
    }

    mutating func markConnected(callUUID: UUID) -> Bool {
        guard var entry = calls[callUUID], entry.phase == .answering else { return false }
        entry.phase = .active
        calls[callUUID] = entry
        return true
    }

    mutating func reconcile(openOfferIds: Set<String>, nowUnixMs: Int64) -> [UUID] {
        let stale = calls.compactMap { callUUID, entry -> UUID? in
            guard entry.phase == .ringing,
                  entry.payload.expiresAtUnixMs <= nowUnixMs
                    || !openOfferIds.contains(entry.payload.offerId)
            else { return nil }
            return callUUID
        }
        for callUUID in stale { calls.removeValue(forKey: callUUID) }
        return stale
    }

    func phase(callUUID: UUID) -> Phase? {
        calls[callUUID]?.phase
    }

    func payload(callUUID: UUID) -> VoipPushPayload? {
        calls[callUUID]?.payload
    }

    func callUUID(offerId: String) -> UUID? {
        calls.first(where: { $0.value.payload.offerId == offerId })?.key
    }

    func firstCallUUID(in phase: Phase) -> UUID? {
        calls.first(where: { $0.value.phase == phase })?.key
    }

    @discardableResult
    mutating func remove(callUUID: UUID) -> VoipPushPayload? {
        calls.removeValue(forKey: callUUID)?.payload
    }

    mutating func removeAll() -> [VoipPushPayload] {
        defer { calls.removeAll() }
        return calls.values.map(\.payload)
    }
}
