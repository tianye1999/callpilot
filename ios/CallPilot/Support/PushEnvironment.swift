import Foundation

enum PushEnvironment {
    static func current(bundle: Bundle = .main) -> ApnsEnvironment {
        let profile = bundle.url(forResource: "embedded", withExtension: "mobileprovision")
            .flatMap { try? Data(contentsOf: $0) }
        return resolve(profile: profile)
    }

    static func resolve(profile: Data?, debug: Bool = isDebug) -> ApnsEnvironment {
        // The signed CMS envelope is binary; decode only its embedded XML plist.
        if let profile,
           let start = profile.range(of: Data("<plist".utf8)),
           let end = profile.range(of: Data("</plist>".utf8), in: start.lowerBound..<profile.endIndex),
           let object = try? PropertyListSerialization.propertyList(
               from: profile.subdata(in: start.lowerBound..<end.upperBound), options: [], format: nil
           ) as? [String: Any],
           let entitlements = object["Entitlements"] as? [String: Any] {
            switch entitlements["aps-environment"] as? String {
            case "development": return .sandbox
            case "production": return .production
            default: break
            }
        }
        return debug ? .sandbox : .production
    }

    static var isDebug: Bool {
        #if DEBUG
        true
        #else
        false
        #endif
    }
}

enum AlertPushConfiguration {
    static var isEnabled: Bool { enabled(value: Bundle.main.object(forInfoDictionaryKey: "AlertPushEnabled")) }

    static func enabled(value: Any?) -> Bool {
        if let text = value as? String { return text == "YES" || text == "true" }
        return value as? Bool == true
    }
}
