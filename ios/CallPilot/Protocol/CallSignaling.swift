import Foundation

enum CallPilotTopic {
    static let control = "callpilot.control"
    static let status = "callpilot.status"
}

enum CallSignalingError: Error, Equatable {
    case invalidNumber
    case invalidDTMF
    case invalidIdempotencyKey
}

enum EdgeCallEvent: Equatable {
    case status(name: String, reason: String?, code: String?)
    case remoteCall(status: String)
}

/// Pure JSON codec for reliable LiveKit data packets. The media layer owns
/// transport; this type owns only the Edge wire schema.
enum CallSignaling {
    private static var numberRE: Regex<Substring> { /^\+?[0-9*#]{1,32}$/ }
    private static var dtmfRE: Regex<Substring> { /^[0-9*#]{1,16}$/ }
    private static var idempotencyRE: Regex<Substring> { /^[A-Za-z0-9_-]{8,64}$/ }

    static func encodeDial(number: String, idempotencyKey: String) throws -> Data {
        guard number.wholeMatch(of: numberRE) != nil else {
            throw CallSignalingError.invalidNumber
        }
        guard idempotencyKey.wholeMatch(of: idempotencyRE) != nil else {
            throw CallSignalingError.invalidIdempotencyKey
        }
        return encode([
            "type": "dial",
            "number": number,
            "idempotency_key": idempotencyKey,
        ])
    }

    static func encodeDTMF(_ digits: String) throws -> Data {
        guard digits.wholeMatch(of: dtmfRE) != nil else {
            throw CallSignalingError.invalidDTMF
        }
        return encode(["type": "dtmf", "digits": digits])
    }

    static func encodeHangup() -> Data {
        encode(["type": "hangup"])
    }

    static func decodeEvent(_ data: Data) -> EdgeCallEvent? {
        guard let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any],
              let type = payload["type"] as? String,
              let status = payload["status"] as? String else { return nil }

        switch type {
        case "status":
            return .status(
                name: status,
                reason: payload["reason"] as? String,
                code: payload["code"] as? String
            )
        case "remote_call":
            return .remoteCall(status: status)
        default:
            return nil
        }
    }

    private static func encode(_ payload: [String: String]) -> Data {
        // All values are locally validated strings, so serialization cannot fail.
        // Keep the fallback fail-closed rather than exposing an optional packet.
        (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data()
    }
}
