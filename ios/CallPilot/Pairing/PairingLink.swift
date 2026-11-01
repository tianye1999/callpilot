import Foundation

/// Desktop/Android format: https://<gateway>/remote_dialer.html#pair=XXXX-XXXX.
enum PairingLink {
    static let defaultGateway = "https://sim-relay.bondings.ai"

    struct Parsed: Equatable {
        let gatewayBase: String?
        let code: String?
    }

    static func parse(_ text: String) -> Parsed {
        let input = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if input.wholeMatch(of: /^[A-Za-z0-9]{4}-?[A-Za-z0-9]{4}$/) != nil {
            return Parsed(gatewayBase: nil, code: normalizeCode(input))
        }
        guard let url = URLComponents(string: input),
              url.scheme?.lowercased() == "https",
              let host = url.host, !host.isEmpty,
              url.port == nil || (1...65535).contains(url.port!) else {
            return Parsed(gatewayBase: nil, code: nil)
        }
        var origin = URLComponents()
        origin.scheme = "https"
        origin.host = host.lowercased()
        origin.port = url.port == 443 ? nil : url.port
        let fragment = url.fragment ?? ""
        let code = fragment.hasPrefix("pair=")
            ? normalizeCode(String(fragment.dropFirst(5))) : nil
        return Parsed(gatewayBase: origin.string, code: code)
    }

    static func formatCode(_ normalized: String) -> String {
        "\(normalized.prefix(4))-\(normalized.dropFirst(4))"
    }

    private static func normalizeCode(_ raw: String) -> String? {
        let cleaned = raw.filter { $0.isLetter || $0.isNumber }.uppercased()
        return cleaned.count == 8 ? cleaned : nil
    }
}
