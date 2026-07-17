import Foundation

enum PairingErrorCopy {
    static func message(code: String, locale: Locale? = nil) -> String {
        let key = switch code {
        case "INVALID_PAIRING": "pair.error.invalid_code"
        case "DEVICE_LIMIT": "pair.error.device_limit"
        case "RATE_LIMITED": "pair.error.rate_limited"
        case "BAD_BASE_URL": "pair.error.bad_gateway"
        default: "pair.error.unavailable"
        }
        return L10n.text(key, locale: locale)
    }
}

enum CallFailureCopy {
    static func message(code: String?, locale: Locale? = nil) -> String {
        let key = switch code {
        case "EDGE_OFFLINE": "call.error.edge_offline"
        case "MODEM_OFFLINE": "call.error.modem_offline"
        case "SIM_NOT_READY": "call.error.sim_not_ready"
        case "SIM_NOT_REGISTERED": "call.error.sim_not_registered"
        case "SERVICE_NUMBER_MISMATCH": "call.error.service_number_mismatch"
        case "LINE_BUSY": "call.error.line_busy"
        case "RATE_LIMITED": "call.error.rate_limited"
        case "TAKEOVER_MEDIA_TIMEOUT", "SESSION_TIMEOUT": "call.error.timeout"
        case "OFFER_UNAVAILABLE": "call.error.offer_unavailable"
        default: "call.error.generic"
        }
        return L10n.text(key, locale: locale)
    }
}
