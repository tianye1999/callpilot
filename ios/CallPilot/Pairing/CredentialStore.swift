import Foundation
import Security

/// 已保存的 hosted 配对(对齐 Android StoredPairing 的 hosted 子集)。
/// 前台版只做 hosted 一条路(Android 的 TUNNEL 本地网关 iOS 暂不做)。
struct StoredPairing: Equatable, Codable {
    let gatewayURL: String
    let displayName: String
    let credential: DeviceCredential
    let edgeId: String
}

/// Keychain 持久化配对凭证(对齐 Android EncryptedSharedPreferences——都用系统级加密存储)。
final class CredentialStore {
    private let service = "ai.bondings.callpilot.pairing"
    private let account = "hosted"

    func load() -> StoredPairing? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let pairing = try? JSONDecoder().decode(StoredPairing.self, from: data) else {
            return nil
        }
        return pairing
    }

    func save(_ pairing: StoredPairing) {
        guard let data = try? JSONEncoder().encode(pairing) else { return }
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    func clear() {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
    }
}
