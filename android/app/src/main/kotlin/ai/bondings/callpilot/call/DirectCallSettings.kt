package ai.bondings.callpilot.call

import android.content.Context

/** 来电接管直连（Fastpath V2）本机开关：未经真机验收前默认关闭，由设置页打开。 */
object DirectCallSettings {
    private const val PREFS = "offer_settings"
    private const val KEY = "direct_takeover"

    fun enabled(context: Context): Boolean =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY, false)

    fun set(context: Context, enabled: Boolean) {
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY, enabled).apply()
    }
}
