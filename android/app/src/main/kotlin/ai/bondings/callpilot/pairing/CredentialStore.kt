package ai.bondings.callpilot.pairing

import ai.bondings.callpilot.protocol.DeviceCredential
import ai.bondings.callpilot.protocol.PairingProtocol
import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** 已配对状态的持久化：网关地址 + 设备凭证，落 EncryptedSharedPreferences。 */
data class StoredPairing(
    val gatewayUrl: String,
    val displayName: String,
    val credential: DeviceCredential,
    val protocol: PairingProtocol = PairingProtocol.TUNNEL,
    val edgeId: String? = null,
)

class CredentialStore(context: Context) {
    private val prefs = EncryptedSharedPreferences.create(
        context,
        "callpilot_pairing",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    fun save(pairing: StoredPairing) {
        prefs.edit()
            .putString(KEY_GATEWAY, pairing.gatewayUrl)
            .putString(KEY_NAME, pairing.displayName)
            .putString(KEY_DEVICE_ID, pairing.credential.deviceId)
            .putString(KEY_SECRET, pairing.credential.secret)
            .putString(KEY_PROTOCOL, pairing.protocol.storedValue)
            .putString(KEY_EDGE_ID, pairing.edgeId)
            .apply()
    }

    fun load(): StoredPairing? {
        val gateway = prefs.getString(KEY_GATEWAY, null) ?: return null
        val deviceId = prefs.getString(KEY_DEVICE_ID, null) ?: return null
        val secret = prefs.getString(KEY_SECRET, null) ?: return null
        val name = prefs.getString(KEY_NAME, "") ?: ""
        val protocol = PairingProtocol.fromStored(prefs.getString(KEY_PROTOCOL, null))
        val edgeId = prefs.getString(KEY_EDGE_ID, null)
        if (protocol == PairingProtocol.HOSTED && edgeId.isNullOrBlank()) return null
        return StoredPairing(gateway, name, DeviceCredential(deviceId, secret), protocol, edgeId)
    }

    /** 解除配对：清凭证但保留最近网关，下次配对免重新粘贴链接。 */
    fun clear() {
        val lastGateway = prefs.getString(KEY_GATEWAY, null)
        prefs.edit().clear().apply()
        lastGateway?.let { prefs.edit().putString(KEY_LAST_GATEWAY, it).apply() }
    }

    fun loadLastGateway(): String? = prefs.getString(KEY_LAST_GATEWAY, null)

    private companion object {
        const val KEY_GATEWAY = "gateway_url"
        const val KEY_NAME = "display_name"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_SECRET = "secret"
        const val KEY_PROTOCOL = "protocol"
        const val KEY_EDGE_ID = "edge_id"
        const val KEY_LAST_GATEWAY = "last_gateway_url"
    }
}
