package ai.bondings.callpilot.call

import android.content.Context
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** 只保存启用水位、opaque messageId 和 revision，不落号码或短信正文。 */
class SmsAlertStore(private val context: Context) {
    private val prefs = context.getSharedPreferences("sms_alerts", Context.MODE_PRIVATE)

    fun load(deviceId: String, now: Long): SmsAlertPolicy {
        if (prefs.getString("device_id", null) != deviceId) clear()
        val saved = runCatching {
            Json.decodeFromString<SmsAlertState>(prefs.getString("state", "{}")!!)
        }.getOrDefault(SmsAlertState())
        return SmsAlertPolicy(saved).also {
            it.pending(emptyList(), now)
            prefs.edit().putString("device_id", deviceId).apply()
            save(it)
        }
    }

    fun save(policy: SmsAlertPolicy) {
        prefs.edit().putString("state", Json.encodeToString(policy.snapshot())).apply()
    }

    fun clear() {
        prefs.edit().clear().apply()
        SmsAlerts.clearNotifications(context)
    }
}
