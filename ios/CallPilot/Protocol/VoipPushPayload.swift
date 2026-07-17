import CoreFoundation
import Foundation

/// Opaque PushKit payload. Caller identity and call content never cross APNs.
struct VoipPushPayload: Equatable {
    let offerId: String
    let callUUID: UUID
    let expiresAtUnixMs: Int64

    private static var offerIdRE: Regex<Substring> {
        /^offer_[A-Za-z0-9_-]{12,80}$/
    }

    static func decode(_ raw: [AnyHashable: Any]) -> VoipPushPayload? {
        guard jsonInt64(raw["v"]) == 1,
              raw["type"] as? String == "inbound.offer",
              let offerId = raw["offerId"] as? String,
              offerId.wholeMatch(of: offerIdRE) != nil,
              let callUUIDString = raw["callUUID"] as? String,
              let callUUID = UUID(uuidString: callUUIDString),
              let expiresAtUnixMs = jsonInt64(raw["expiresAtUnixMs"]),
              expiresAtUnixMs > 0 else { return nil }
        return VoipPushPayload(
            offerId: offerId,
            callUUID: callUUID,
            expiresAtUnixMs: expiresAtUnixMs
        )
    }

    private static func jsonInt64(_ value: Any?) -> Int64? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let integer = number.int64Value
        guard number.doubleValue.isFinite,
              number.doubleValue == Double(integer) else { return nil }
        return integer
    }
}
