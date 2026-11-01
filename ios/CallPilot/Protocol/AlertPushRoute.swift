import CoreFoundation
import Foundation

struct AlertPushRoute: Equatable {
    enum Destination { case messages, records }
    let id = UUID()
    let destination: Destination

    static func decode(_ raw: [AnyHashable: Any]) -> AlertPushRoute? {
        guard let version = raw["v"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(), version == 1,
              raw["type"] as? String == "device.notify" else { return nil }
        switch raw["kind"] as? String {
        case "sms": return AlertPushRoute(destination: .messages)
        case "call_digest", "missed_call": return AlertPushRoute(destination: .records)
        default: return nil
        }
    }
}
