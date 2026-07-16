import Foundation

enum L10n {
    static func text(
        _ key: String,
        locale: Locale? = nil
    ) -> String {
        guard let locale else {
            return Bundle.main.localizedString(forKey: key, value: key, table: nil)
        }
        let candidates = [
            locale.identifier,
            locale.language.languageCode?.identifier,
        ].compactMap { $0 }
        for candidate in candidates {
            guard let path = Bundle.main.path(forResource: candidate, ofType: "lproj"),
                  let bundle = Bundle(path: path) else { continue }
            return bundle.localizedString(forKey: key, value: key, table: nil)
        }
        return key
    }

    static func format(_ key: String, _ arguments: CVarArg...) -> String {
        String(
            format: text(key),
            locale: .current,
            arguments: arguments
        )
    }
}
