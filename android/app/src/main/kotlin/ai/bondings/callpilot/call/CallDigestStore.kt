package ai.bondings.callpilot.call

import android.content.Context
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** 只保存启用水位与 opaque callId，不落号码、身份或摘要正文。 */
class CallDigestStore(private val context: Context) {
    private val prefs = context.getSharedPreferences("call_digests", Context.MODE_PRIVATE)

    fun load(deviceId: String, now: Long): CallDigestPolicy {
        if (prefs.getString("device_id", null) != deviceId) clear()
        val saved = runCatching {
            Json.decodeFromString<CallDigestState>(prefs.getString("state", "{}")!!)
        }.getOrDefault(CallDigestState())
        return CallDigestPolicy(saved).also {
            it.pending(emptyList(), now)
            prefs.edit().putString("device_id", deviceId).apply()
            save(it)
        }
    }

    fun save(policy: CallDigestPolicy) {
        prefs.edit().putString("state", Json.encodeToString(policy.snapshot())).apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
        CallDigestAlerts.clearNotifications(context)
    }
}
