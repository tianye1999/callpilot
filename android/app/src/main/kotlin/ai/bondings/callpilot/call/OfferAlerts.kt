package ai.bondings.callpilot.call

import ai.bondings.callpilot.MainActivity
import ai.bondings.callpilot.R
import ai.bondings.callpilot.protocol.InboundOffer
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import androidx.annotation.MainThread
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** 所有入口在主线程串行更新，避免 Activity 拒绝后被服务的旧响应重新提醒。 */
@MainThread
class OfferAlerts private constructor(private val context: Context) {
    private val notifications = context.getSystemService(NotificationManager::class.java)
    private val prefs = context.getSharedPreferences("offer_alerts", Context.MODE_PRIVATE)
    private var saved = runCatching {
        Json.decodeFromString<Map<String, OfferAlertRecord>>(prefs.getString("handled", "{}")!!)
    }.getOrDefault(emptyMap())
    private var policy = OfferAlertPolicy(saved)
    private var offers = emptyList<InboundOffer>()
    private var foreground = false
    private var busy = false

    init {
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, context.getString(R.string.offer_channel), NotificationManager.IMPORTANCE_HIGH).apply {
                setSound(
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
                )
                enableVibration(true)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            },
        )
        // 进程重建后先清理系统残留通知，已提醒记录仍保留到过期。
        clearNotifications()
    }

    fun bindPairing(deviceId: String) {
        if (prefs.getString("device_id", null) != deviceId) {
            reset()
            prefs.edit().putString("device_id", deviceId).apply()
        }
    }

    fun setForeground(value: Boolean) {
        foreground = value
        // 离开 App 不凭旧快照发新通知，等服务下一次成功查询。
        reconcile(allowNew = false)
    }

    fun update(available: List<InboundOffer>) {
        offers = available
        reconcile(allowNew = true)
    }

    fun tick(isBusy: Boolean) {
        busy = isBusy
        reconcile(allowNew = false)
    }

    fun isDismissed(offerId: String): Boolean = policy.isDismissed(offerId)

    fun presented(offer: InboundOffer) {
        policy.presented(offer)
        persist()
    }

    fun dismiss(offer: InboundOffer) {
        policy.dismiss(offer)
        reconcile(allowNew = false)
    }

    fun reset() {
        clearNotifications()
        policy = OfferAlertPolicy()
        persist()
    }

    fun clearNotifications() {
        offers = emptyList()
        notifications.activeNotifications.filter { it.notification.channelId == CHANNEL_ID }.forEach {
            notifications.cancel(it.tag, it.id)
        }
    }

    private fun reconcile(allowNew: Boolean) {
        val now = System.currentTimeMillis()
        val changes = policy.update(
            offers, now, foreground, busy,
            notificationsAllowed = notifications.areNotificationsEnabled() &&
                notifications.getNotificationChannel(CHANNEL_ID)?.importance != NotificationManager.IMPORTANCE_NONE,
            allowNew = allowNew,
        )
        changes.cancel.forEach { notifications.cancel(it, ALERT_ID) }
        changes.notify.forEach { offer ->
            val intent = Intent(context, MainActivity::class.java)
                .setAction(ACTION_INCOMING_OFFER)
                .setData(Uri.parse("callpilot://inbound/${offer.offerId}"))
                .putExtra(EXTRA_OFFER_ID, offer.offerId)
                .putExtra(EXTRA_EXPIRES_AT, offer.expiresAt)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            val pending = PendingIntent.getActivity(
                context, 1, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            fun notification(fullScreen: Boolean): Notification = Notification.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_callpilot_notification)
                .setContentTitle(context.getString(R.string.offer_notification_title))
                .setContentText(context.getString(R.string.offer_notification_body))
                .setCategory(Notification.CATEGORY_CALL)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setContentIntent(pending)
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setTimeoutAfter((offer.expiresAt - now).coerceAtLeast(1))
                .apply { if (fullScreen) setFullScreenIntent(pending, true) }
                .build()
            try {
                notifications.notify(offer.offerId, ALERT_ID, notification(canUseFullScreenIntent(context)))
            } catch (_: SecurityException) {
                // 授权可能在检查后被撤销；只退化通知，不从服务直接拉 Activity。
                try {
                    notifications.notify(offer.offerId, ALERT_ID, notification(false))
                } catch (_: SecurityException) {
                    policy.notificationFailed(offer.offerId)
                }
            }
        }
        persist()
    }

    private fun persist() {
        val snapshot = policy.snapshot()
        if (snapshot == saved) return
        saved = snapshot
        prefs.edit().putString("handled", Json.encodeToString(snapshot)).apply()
    }

    companion object {
        const val ACTION_INCOMING_OFFER = "ai.bondings.callpilot.INCOMING_OFFER"
        const val EXTRA_OFFER_ID = "offer_id"
        const val EXTRA_EXPIRES_AT = "offer_expires_at"
        private const val CHANNEL_ID = "callpilot_incoming_offers"
        private const val ALERT_ID = 1003
        // 只保存 applicationContext，不持有 Activity / Service。
        @SuppressLint("StaticFieldLeak")
        private var instance: OfferAlerts? = null

        fun get(context: Context): OfferAlerts = instance ?: OfferAlerts(context.applicationContext).also { instance = it }

        fun canUseFullScreenIntent(context: Context): Boolean = Build.VERSION.SDK_INT < 34 ||
            context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()
    }
}
